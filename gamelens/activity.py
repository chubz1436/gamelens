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

# How far back "already moving" looks, in seconds. Longer catches slower
# animation and costs nothing but memory (one small bool map per frame fed).
ACTIVITY_WINDOW = 2.0

# Mean absolute difference, 0-255, per tile per channel between two consecutive
# frames fed, above which the tile moved. Raw captured frames have no encoder
# noise, so a still tile scores exactly 0.
ACTIVITY_THRESHOLD = 1.0

# Consecutive samples miss slow drift, so snapshot() also compares the newest
# sample with the oldest in the window; see there.

# Tiles around a moving one that count as moving too: motion sampled at 20 Hz
# can move further than a tile between samples.
ACTIVITY_DILATE = 1


class ActivityMap:
    """Rolling record of which tiles changed recently. Thread-safe."""

    def __init__(self, *, window: float = ACTIVITY_WINDOW, tile: int = ACTIVITY_TILE,
                 threshold: float = ACTIVITY_THRESHOLD,
                 dilate: int = ACTIVITY_DILATE) -> None:
        self.window = window
        self.tile = tile
        self.threshold = threshold
        self.dilate = dilate
        self._lock = threading.Lock()
        self._last_id: int | None = None
        self._last_small: np.ndarray | None = None
        self._maps: deque = deque()

    def feed(self, array: np.ndarray, frame_id: int, now: float | None = None) -> None:
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
            prev, self._last_small = self._last_small, small
            if prev is None or prev.shape != small.shape:
                # A new size is a new layout; motion measured against the old
                # one means nothing.
                self._maps.clear()
                return
            moved = np.abs(small - prev).max(axis=2) > self.threshold
            self._maps.append((now, moved, small))
            while self._maps and now - self._maps[0][0] > self.window:
                self._maps.popleft()

    def snapshot(self, now: float | None = None) -> np.ndarray | None:
        """Tiles that moved within the window, dilated; None with no history.

        None means "nothing known to be moving" and is treated as all still,
        which is the strict direction: the whole screen gets scored.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            kept = [(m, s) for t, m, s in self._maps if now - t <= self.window]
        if not kept:
            return None
        moving = np.logical_or.reduce([m for m, _ in kept])
        # Drift too slow to cross the threshold between two samples -- a
        # translucent panel over a slowly turning panorama -- still adds up
        # across the window, so the ends are compared as well.
        moving |= np.abs(kept[-1][1] - kept[0][1]).max(axis=2) > self.threshold
        if self.dilate:
            k = 2 * self.dilate + 1
            moving = cv2.dilate(moving.astype(np.uint8), np.ones((k, k), np.uint8)) > 0
        return moving
