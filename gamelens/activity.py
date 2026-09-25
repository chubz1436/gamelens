"""Which parts of the screen were already moving, before anyone acted (GL-042).

Rebinding a click to a newer frame (GL039-R3) checks two things: the pixels at
the click point, and a whole-screen score that catches a screen swap in which
the click point happens to survive -- Bedrock's Settings and Play screens put
an identical back arrow at the identical spot. The whole-screen score could not
tell that swap from a screen that animates by itself: Bedrock's Play screen
draws its menus over a moving panorama, which scored 6.5 to 14 over one to ten
seconds against a limit of 2.0, so every late click on it was refused while
the buttons had not changed by a single pixel.

What separates the two is not how much changed but where. A panorama changes
what was already changing; a swap changes what was still. So the server keeps a
map of the tiles that changed across the last couple of seconds of frames, the
record of an observation keeps that map as it stood when the image was handed
out, and the whole-screen score is taken over the still part only. A screen
with nothing moving is scored exactly as before, over all of it.

Measured on the VM (Bedrock 26.51, 1280-wide transport): the same screen
scored at most 2.45 over twenty seconds, any swap between five real screens at
least 41.

"Already moving" means moving throughout, not moved once (GL042-I01, Codex): a
page transition changes most of the screen in a fraction of a second and then
stops. Counted as motion, it would hide exactly the content a second swap
replaces. So the window is cut into ACTIVITY_BUCKETS parts and a tile is
moving only when it moved in at least ACTIVITY_NEED of them. On recorded
streams: the Play panorama still marked (global 0.07-0.20 on the rest), an
instant pause-to-world swap inside the window marked nothing.

And the map describes the frame it was handed out with (GL042-I02): only
samples up to that frame's id, from its capture session, and a new session or
native size starts the history again.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import cv2
import numpy as np

# Tile edge in native frame pixels. A tile is the unit of "moving": fine enough
# that a panorama's edge does not swallow the menu beside it, coarse enough to
# be cheap -- a 1557x873 frame is 97x54 tiles.
ACTIVITY_TILE = 16

# How far back "already moving" looks, in seconds, and how it is divided. A tile
# is moving when it moved in at least ACTIVITY_NEED of ACTIVITY_BUCKETS equal
# parts of the window -- persistent motion, not one burst -- one of them the
# latest: something that has just stopped is not moving (GL042-RV02-I01).
ACTIVITY_WINDOW = 2.0
ACTIVITY_BUCKETS = 4
ACTIVITY_NEED = 3

# Mean absolute difference, 0-255, per tile per channel, above which a tile
# moved: between consecutive samples, or between the ends of a bucket (slow
# drift -- a translucent panel over a turning panorama -- stays under it from
# one 50 ms sample to the next). Raw captured frames have no encoder noise, so a
# still tile scores exactly 0.
ACTIVITY_THRESHOLD = 1.0

# Tiles around a moving one that count as moving too: motion sampled at 20 Hz
# can move further than a tile between samples.
ACTIVITY_DILATE = 1


class ActivityMap:
    """Rolling record of which tiles keep changing. Thread-safe."""

    def __init__(self, *, window: float = ACTIVITY_WINDOW, tile: int = ACTIVITY_TILE,
                 threshold: float = ACTIVITY_THRESHOLD, dilate: int = ACTIVITY_DILATE,
                 buckets: int = ACTIVITY_BUCKETS, need: int = ACTIVITY_NEED) -> None:
        self.window = window
        self.tile = tile
        self.threshold = threshold
        self.dilate = dilate
        self.buckets = buckets
        self.need = need
        self._lock = threading.Lock()
        self._last_id: int | None = None
        self._source: tuple | None = None     # (session_id, native h, native w)
        self._samples: deque = deque()        # (time, frame_id, small tile image)

    def feed(self, array: np.ndarray, frame_id: int, session_id: int = 0,
             now: float | None = None) -> None:
        """Take in one captured frame (BGR or BGRA). A frame id seen already is
        ignored, so polling faster than capture does not dilute anything."""
        now = time.monotonic() if now is None else now
        h, w = array.shape[:2]
        gw, gh = max(1, w // self.tile), max(1, h // self.tile)
        small = cv2.resize(np.ascontiguousarray(array[:, :, :3]), (gw, gh),
                           interpolation=cv2.INTER_AREA).astype(np.int16)
        with self._lock:
            if frame_id == self._last_id:
                return
            self._last_id = frame_id
            source = (session_id, h, w)
            if source != self._source:
                # Another capture session or another size: motion measured
                # against the old one means nothing here.
                self._samples.clear()
                self._source = source
            self._samples.append((now, frame_id, small))
            while self._samples and now - self._samples[0][0] > self.window:
                self._samples.popleft()

    def snapshot(self, until_frame_id: int | None = None, session_id: int | None = None,
                 size: tuple[int, int] | None = None,
                 now: float | None = None) -> np.ndarray | None:
        """Tiles moving throughout the window, dilated; None with no usable history.

        Only samples up to ``until_frame_id`` (the frame being handed out),
        from ``session_id`` and at native ``size`` (h, w) count: the encoder can
        run ahead of the poller across a resize (GL042-RV02-I02). None means
        "nothing known to be moving" and is treated as all still, which is the
        strict direction.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._source is None:
                return None
            if session_id is not None and self._source[0] != session_id:
                return None
            if size is not None and tuple(self._source[1:]) != tuple(size):
                return None
            kept = [(t, s) for t, fid, s in self._samples
                    if now - t <= self.window
                    and (until_frame_id is None or fid <= until_frame_id)]
        if len(kept) < 2:
            return None
        end = kept[-1][0]
        span = self.window / self.buckets
        count = np.zeros(kept[0][1].shape[:2], np.int16)
        latest = np.zeros(kept[0][1].shape[:2], bool)
        for i in range(self.buckets):
            lo, hi = end - self.window + i * span, end - self.window + (i + 1) * span
            part = [s for t, s in kept if lo <= t <= hi]
            if len(part) < 2:
                continue            # nothing seen then: not evidence of motion
            moved = np.abs(part[-1] - part[0]).max(axis=2) > self.threshold
            for a, b in zip(part, part[1:]):
                moved |= np.abs(b - a).max(axis=2) > self.threshold
            count += moved
            if i == self.buckets - 1:
                latest = moved
        moving = (count >= self.need) & latest
        if self.dilate:
            k = 2 * self.dilate + 1
            moving = cv2.dilate(moving.astype(np.uint8), np.ones((k, k), np.uint8)) > 0
        return moving
