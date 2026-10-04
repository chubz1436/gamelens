"""The runtime: one object that owns every part and wires them together."""

from __future__ import annotations

import logging
import secrets
import statistics
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace

import cv2
import numpy as np

from gamelens.run_metrics import RunMetrics
from gamelens.run_store import LocalRunStore
from gamelens.perception_bridge import PerceptionViews
from gamelens.activity import ActivityMap
from gamelens.arbiter import ActionRejected, Arbiter, Rejection
from gamelens.capture import Backend, CaptureSupervisor
from gamelens.coords import GeometryTracker
from gamelens.input import InputExecutor
from gamelens.safety import SafetySupervisor
from gamelens.target import parse_anchor, same_at_anchor
from gamelens.server import (
    ENCODE_FAILED, NO_FRAME, ActionLog, Encoded, Tokens, encode_jpeg,
)
from gamelens.windows import WindowInfo, describe, find_window

log = logging.getLogger(__name__)

# How long an HTTP caller waits to be told what actually happened. A click is a
# move, a settle and a hold -- roughly 200ms with the current defaults -- so
# this covers the normal case with room for a shallow queue, and a caller that
# outlasts it is told "pending" rather than handed a guess.
DISPATCH_WAIT = 1.5

# Mean absolute difference, 0-255, below which a thumbnail pair is called still.
# JPEG-free (the thumbnail comes off the raw frame) so the floor only has to
# clear sensor-level jitter, not compression noise.
CHURN_FLOOR = 1.0

# The thumbnail is small on purpose: the question is "did the screen move",
# which survives aggressive downscaling, and the measurement sits inside the
# dispatch path where cost is latency.
CHURN_SIZE = (64, 36)

# Sampled this long after the executor finishes, not at the moment it finishes.
# During a mining hold the game animates cracks on the block, so the screen is
# busy for the whole action whether or not anything breaks; comparing across the
# action therefore reports motion in both cases and separates nothing. After the
# button releases the transient ends -- cracks vanish, a broken block does not
# come back -- so the comparison has to straddle the action, not overlap it.
CHURN_SETTLE = 0.15

# The widest settle a caller may ask for. The knob exists so a caller that knows
# its target's animation timing can spend less than the default; it is not a way
# to park a worker thread for a minute.
MAX_SETTLE = 1.0

# Rebinding a click to a newer frame (GL039-R3). The click point's own
# neighbourhood must be unchanged, compared in colour at the resolution the
# agent was shown; the whole-image score is a second, coarser check and never
# the authorisation for the target itself. 33x33 transport pixels is about 40
# frame pixels at 1557 wide -- larger than an inventory slot, so an item that
# arrived, left or changed in the clicked slot fails it. JPEG encoding is
# deterministic for identical pixels, so these only absorb re-render jitter.
PATCH_HALF = 16                # 33x33
PATCH_MAX_DIFF = 12            # per pixel, per channel, 0-255
PATCH_MEAN_DIFF = 1.0
GLOBAL_MAD = 2.0

# The whole-screen score is taken over the tiles that were not already moving
# when the image was handed out (GL-042, see activity.py). Below this fraction
# of still screen there is too little left to judge a swap by, and the score
# falls back to the whole image -- the strict answer, as before.
MIN_STILL = 0.10


def _expected_duration(action) -> float:
    """How long the action's own dwells will take, at minimum.

    Read off the steps rather than assumed from the kind, so it stays right
    when a new kind of action is added.
    """
    from gamelens.input import Dwell

    return sum(s.seconds for s in action.steps if isinstance(s, Dwell))


@dataclass(frozen=True)
class Dispatch:
    """The two separate things a caller needs to know about one action.

    ``verdict`` is acceptance: ``"ok"`` means the arbiter agreed the world was
    still the one the action was reasoned about. ``outcome`` is what the
    executor did with it afterwards -- ``sent``, ``dry``, ``denied``,
    ``cancelled``, ``error``, or ``pending`` when the caller stopped waiting.

    They are separate because they disagree, routinely. An action can be
    accepted and then refused at press time because focus moved in the
    intervening queue and dwell. Reporting only the verdict is how a driver ends
    up printing "ok" for a click the game never saw.
    """

    verdict: str
    outcome: str
    detail: str = ""
    action_id: int | None = None
    churn: float | None = None
    # The newest frame id at the instant the executor finished injecting, or
    # None when nothing was injected. Any frame with a larger id was captured
    # after the input existed; `/frame.jpg?after=` waits for one. It is NOT a
    # promise the game has drawn the result yet -- games render a frame or
    # more behind their input, which is what `frames=` is for.
    after_frame: int | None = None
    # How the action was bound when the caller asked for rebinding: which
    # observation it ran on, and for a click the patch scores that allowed it
    # or refused it. None when rebinding was not asked for.
    binding: dict | None = None
    completed_steps: int | None = None
    injected_steps: int | None = None
    last_completed_step: int | None = None
    partial: bool = False
    metrics_action_id: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the action was accepted *and* not refused afterwards."""
        return self.verdict == "ok" and self.outcome in ("sent", "dry", "pending")

    @property
    def changed_anything(self) -> bool | None:
        """Whether the screen moved at all across the action, or None if unmeasured.

        Deliberately not folded into ``ok``. This is evidence about the world,
        not a verdict about the action, and the two must not be confused: a
        correct action can leave the screen still -- walking into a wall -- and
        a refused one can sit in front of a screen full of falling rain.
        """
        return None if self.churn is None else self.churn >= CHURN_FLOOR

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "outcome": self.outcome,
            "detail": self.detail,
            "action_id": self.action_id,
            "completed_steps": self.completed_steps,
            "injected_steps": self.injected_steps,
            "last_completed_step": self.last_completed_step,
            "partial": self.partial,
            "churn": self.churn,
            "after_frame": self.after_frame,
            **({"metrics_action_id": self.metrics_action_id} if self.metrics_action_id else {}),
            **(self.binding or {}),
        }


class ObservationRegistry:
    """Immutable records of what the server handed out, and when.

    An HTTP caller cannot be trusted to describe its own view -- not because it
    is hostile, but because it does not know. It received a picture; it does not
    know whether capture has since stalled, whether a reflex preempted, or what
    scale the transport applied. Building the provenance when /act arrives reads
    all of that from *now*, which is exactly the staleness the arbiter exists to
    catch. So the record is made when the image is issued, and /act names it.
    """

    def __init__(self, capacity: int = 16, ttl: float = 120.0) -> None:
        self._records: OrderedDict = OrderedDict()
        self._stream_records: OrderedDict = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl
        self._lock = threading.Lock()

    @property
    def retention_seconds(self) -> float:
        return self._ttl

    def issue(self, observation, *, jpeg: bytes | None = None,
              quality: int | None = None, moving=None, retain: bool = True) -> str:
        """Record what was handed out. ``jpeg`` and ``quality`` are the exact
        image the caller received, kept so a later rebind can compare the
        pixels the decision was made on (GL039-R3); ``moving`` is which tiles
        were already animating at that moment (GL-042). They live and die with
        the record, so memory is bounded by the capacity."""
        token = secrets.token_urlsafe(9)
        now = time.monotonic()
        with self._lock:
            # Agent snapshots survive model/tool round trips. A dashboard stream
            # must not evict the exact picture an agent is still deciding on.
            records = self._records if retain else self._stream_records
            capacity, ttl = (self._capacity, self._ttl) if retain else (256, 5.0)
            records[token] = (observation, now, jpeg, quality, moving)
            while len(records) > capacity:
                records.popitem(last=False)
            # Opportunistic expiry; bounded work per issue.
            for key in [k for k, (_, t, *_rest) in list(records.items())[:8]
                        if now - t > ttl]:
                records.pop(key, None)
        return token

    def resolve(self, token: str):
        record = self.record(token)
        return record[0] if record else None

    def record(self, token: str):
        """``(observation, jpeg, quality, moving)`` for a live record, else None."""
        with self._lock:
            records, ttl = self._records, self._ttl
            entry = records.get(token)
            if entry is None:
                records, ttl = self._stream_records, 5.0
                entry = records.get(token)
            if entry is None:
                return None
            observation, issued_at, jpeg, quality, moving = entry
            if time.monotonic() - issued_at > ttl:
                records.pop(token, None)
                return None
            return observation, jpeg, quality, moving


def same_at_click(shown_jpeg: bytes, fresh_jpeg: bytes, x: float, y: float,
                  moving: np.ndarray | None = None) -> dict:
    """Is the screen at ``(x, y)`` still what the agent was shown?

    Both images are at the transport resolution the agent saw, and ``x, y`` is
    in those pixels. ``moving`` is the tile map from the observation's record:
    the whole-screen score skips those tiles, unless less than MIN_STILL of the
    screen would be left. The patch is never masked -- a click on something
    moving has to find it where it was. Returns the scores, ``still`` (the
    fraction the global score was taken over) and ``ok``; never raises for bad
    input, it answers no.
    """
    out = {"patch_max": None, "patch_mean": None, "global_mad": None, "still": None,
           "ok": False}
    try:
        a = cv2.imdecode(np.frombuffer(shown_jpeg, np.uint8), cv2.IMREAD_COLOR)
        b = cv2.imdecode(np.frombuffer(fresh_jpeg, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return out
    if a is None or b is None or a.shape != b.shape:
        return out
    h, w = a.shape[:2]
    cx, cy = int(round(x)), int(round(y))
    if not (0 <= cx < w and 0 <= cy < h):
        return out
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    patch = diff[max(0, cy - PATCH_HALF):cy + PATCH_HALF + 1,
                 max(0, cx - PATCH_HALF):cx + PATCH_HALF + 1]
    out["patch_max"] = int(patch.max())
    out["patch_mean"] = round(float(patch.mean()), 3)
    still = None
    if moving is not None and moving.any():
        try:
            mask = cv2.resize(moving.astype(np.uint8), (w, h),
                              interpolation=cv2.INTER_NEAREST) == 0
        except Exception:
            return out
        if mask.mean() >= MIN_STILL:
            still = mask
        out["still"] = round(float(mask.mean()), 3)
    else:
        out["still"] = 1.0
    scored = diff if still is None else diff[still]
    out["global_mad"] = round(float(scored.mean()), 3)
    out["ok"] = (out["patch_max"] <= PATCH_MAX_DIFF
                 and out["patch_mean"] <= PATCH_MEAN_DIFF
                 and out["global_mad"] <= GLOBAL_MAD)
    return out


def _button(name: str):
    """Map a caller's word to a mouse button, refusing anything else.

    A typo must not silently become a left click: the difference between the
    two buttons in an inventory is the difference between moving a stack and
    splitting it, and between mining a block and placing one.
    """
    from gamelens.input import button_from_name

    return button_from_name(name)


def session_feedback(target: dict | None, capture: dict, safety: dict, guard: str) -> dict:
    """Explain the current interlock result; never grants input authority."""
    if safety.get("killed"):
        status, message = "stopped", "Stopped. Restart GameLens for a new session."
    elif target is None:
        status, message = "no-target", "Target window is gone. Start a new session on the game."
    elif safety.get("executor", {}).get("unreleased"):
        status, message = "blocked", "An input release failed. New input is blocked."
    elif not capture.get("healthy"):
        status, message = "no-capture", "Capture is unavailable. Wait for a healthy game frame."
    elif not safety.get("armed"):
        status, message = "disarmed", "Operator: Arm to test in dry-run, then Go live."
    elif not target.get("foreground"):
        status, message = "needs-focus", "Bring the game to the foreground before acting."
    elif guard != "ok":
        status, message = "blocked", f"Input blocked: {guard}."
    elif safety.get("dry_run"):
        status, message = "dry-run", "Dry-run: actions are simulated; no input is injected."
    else:
        status, message = "live", "Live input is enabled. Inspect the game to verify each result."
    return {"status": status, "message": message, "input_ready": status == "live",
            "guard": guard, "shared_input": True}


class GameLens:
    """Owns capture, geometry, safety, input and the arbiter for one target."""

    def __init__(
        self,
        target: str | WindowInfo,
        *,
        port: int = 8777,
        rate: float = 10.0,
        pool_depth: int = 4,
        backend: Backend | None = None,
        metrics_directory=None,
    ) -> None:
        self.target: WindowInfo = (
            target if isinstance(target, WindowInfo) else find_window(target)
        )
        self.port = port
        self.tokens = Tokens()
        self.log = ActionLog()
        self.run_metrics = RunMetrics()
        self.run_id = self.run_metrics.begin_run()
        self.run_store = LocalRunStore(base_directory=metrics_directory, enabled=metrics_directory is not None)
        self.perception_views = PerceptionViews()
        self._metrics_context = threading.local()
        from gamelens.reviewed_learning import ReviewedLearningStore
        self.reviewed_learning = ReviewedLearningStore()


        self.capture = CaptureSupervisor(
            self.target, pool_depth=pool_depth, forced=backend
        )
        self.geometry = GeometryTracker(self.target.hwnd)
        from gamelens.recording import Recorder
        self.recorder = Recorder(self._recording_frame)
        self.safety = SafetySupervisor(self.target.hwnd, rate=rate)
        self.executor = InputExecutor(self.safety)
        self.arbiter = Arbiter(self.capture, self.geometry, self.executor)

        self._frame_times: deque = deque(maxlen=120)
        self._frame_ages: deque = deque(maxlen=120)
        self._last_frame_id = 0
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

        self.observations = ObservationRegistry()
        self.activity = ActivityMap()

        self.agent = None          # set by attach_agent
        self._agent_intent = ""

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.capture.start()
        self.safety.start()
        self.executor.start()
        self._poller = threading.Thread(
            target=self._poll, name="gamelens-poller", daemon=True
        )
        self._poller.start()
        log.info("GameLens ready on %r (hwnd %d)", self.target.title, self.target.hwnd)

    def stop(self) -> None:
        # Stop/release input before recorder finalization or thread joins can wait.
        self.safety.kill("runtime shutdown")
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            try:
                recorder.stop()
            except ValueError:
                log.error("Recorder still finishing during shutdown")
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=1.0)
        if self.agent:
            self.agent.stop()
        self.executor.shutdown()
        self.capture.stop()
        self.safety.shutdown()
        self._metrics_call("finish_run", status="completed")
        self.checkpoint_metrics()

    def _poll(self) -> None:
        """Track geometry and frame statistics.

        Geometry is refreshed here rather than per action so the generation
        counter advances promptly when the window moves, which is what makes an
        in-flight action's generation check meaningful.
        """
        while not self._stop.wait(0.05):
            try:
                self.geometry.refresh()
            except Exception:
                log.debug("geometry refresh failed", exc_info=True)

            frame = self.capture.frames.acquire()
            if frame is None:
                continue
            try:
                if frame.frame_id != self._last_frame_id:
                    self._last_frame_id = frame.frame_id
                    self._frame_times.append(time.monotonic())
                    try:
                        self.activity.feed(frame.array, frame.frame_id,
                                           frame.session_id)
                    except Exception:
                        # Without a map the rebind scores the whole screen:
                        # stricter, never looser. Not worth losing the poller.
                        log.debug("activity feed failed", exc_info=True)
                self._frame_ages.append(frame.age() * 1000)
            finally:
                frame.release()

    # --- frames -----------------------------------------------------------

    def _thumbnail(self) -> "np.ndarray | None":
        """A tiny greyscale copy of the newest frame, or None if there is none.

        The copy matters. Frames are leased from a refcounted pool and the
        buffer is reused the moment the lease is released, so anything kept
        beyond the ``finally`` would be silently overwritten by a later frame --
        and a before/after comparison against a buffer that has become the
        after would read as no change at all.
        """
        try:
            frame = self.capture.frames.acquire()
        except Exception:
            log.debug("thumbnail: no capture to sample", exc_info=True)
            return None
        if frame is None:
            return None
        try:
            small = cv2.resize(
                frame.array[:, :, :3], CHURN_SIZE, interpolation=cv2.INTER_AREA
            )
            return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
        except Exception:
            log.debug("thumbnail failed", exc_info=True)
            return None
        finally:
            frame.release()

    def latest_frame_id(self) -> int:
        """Id of the newest published frame, 0 before the first. Cheap and
        non-blocking, so the HTTP layer can poll it from the event loop."""
        return self.capture.frames.latest_id()

    def encode_frame(self, quality: int = 70, *, min_frame_id: int | None = None,
                     retain: bool = True, crop=None):
        """Encode the newest frame and register what was handed out. Never blocks.

        Returns ``Encoded`` -- whose ``frame_id`` is read off the very frame
        that was encoded, not re-read from the slot, which may already hold a
        newer one -- or ``NO_FRAME`` when the slot is empty or holds nothing
        new enough (a failover ``clear()`` lands here too), or
        ``ENCODE_FAILED`` when a frame was leased and could not be encoded.
        The caller needs those two apart: the first is worth waiting through,
        the second would only fail the same way again.

        The record captures the transport scale, so a coordinate in the
        delivered image can be mapped back to the native frame -- without it, a
        1920-wide frame served at 1280 would put every click two thirds of the
        way to where it belonged.
        """
        frame = self.capture.frames.acquire(timeout=0)
        if frame is None:
            return NO_FRAME
        try:
            if min_frame_id is not None and frame.frame_id < min_frame_id:
                return NO_FRAME
            if not self._from_live_backend(frame):
                return NO_FRAME
            jpeg, scale = encode_jpeg(frame.array, quality=quality)
            observation = self.arbiter.observation_for(frame, scale=scale)
            token = self.observations.issue(observation, jpeg=jpeg, quality=quality,
                                            retain=retain, moving=self.activity.snapshot(
                                                frame.frame_id, frame.session_id,
                                                (frame.height, frame.width)))
            # Again after the encode: the backend can be dropped while it runs,
            # and a frame of a dropped capture is not to be shown (GL041-RV02-I01).
            if not self._from_live_backend(frame):
                return NO_FRAME
            self._metrics_call("record_observation", token, observation, source="snapshot" if retain else "stream")
            if crop is not None:
                from gamelens.perception import encode_perception
                perception = encode_perception(frame.array, observation, crop=crop, quality=quality)
                if not self._from_live_backend(frame):
                    return NO_FRAME
                view_token = self.perception_views.issue(token, perception.view)
                self._metrics_call("record_observation", view_token, observation, source="crop")
                return Encoded(perception.jpeg, view_token, frame.frame_id, perception.view.metadata())
            return Encoded(jpeg, token, frame.frame_id)
        except Exception:
            log.exception("frame encode failed")
            return ENCODE_FAILED
        finally:
            frame.release()

    def _from_live_backend(self, frame) -> bool:
        """Is ``frame`` from the backend in service now (GL041-I02)?

        A backend being dropped is retired before its frame leaves the slot, so
        for a moment the slot can hand out a frame of a capture that is no
        longer running -- more often now that a healthy fallback is dropped to
        step back up. The arbiter already refuses actions on it; this keeps it
        from being shown at all.
        """
        backend = self.capture.backend
        return (backend is not None and not getattr(backend, "retired", False)
                and backend.session_id == frame.session_id)

    def encode_latest(self, quality: int = 70) -> tuple[bytes | None, str]:
        """``encode_frame`` for callers that only want ``(jpeg, observation_id)``."""
        result = self.encode_frame(quality)
        if isinstance(result, Encoded):
            return result.jpeg, result.observation_id
        return None, ""

    def _recording_frame(self):
        frame = self.capture.frames.acquire(timeout=0)
        if frame is None:
            return None
        try:
            if not self._from_live_backend(frame) or frame.age() > 0.5:
                return None
            return frame.array[:, :, :3].copy(), frame.session_id
        finally:
            frame.release()

    def _metrics_call(self, method, *args, **kwargs):
        metrics = getattr(self, "run_metrics", None)
        if metrics is None:
            return None
        try:
            return getattr(metrics, method)(self.run_id, *args, **kwargs)
        except Exception:
            log.debug("passive run metrics unavailable", exc_info=True)
            return None

    def checkpoint_metrics(self):
        store = getattr(self, "run_store", None)
        if store is None:
            return {"saved": False, "reason": "disabled"}
        try:
            return store.save(self.run_metrics.snapshot(self.run_id))
        except Exception:
            log.warning("run checkpoint unavailable")
            return {"saved": False, "reason": "storage unavailable"}

    def dispatch_with_metrics(self, fn, context, **kwargs):
        self._metrics_context.value = context
        try:
            result = fn(**kwargs)
            if result.action_id is None:
                action_id = "request-" + secrets.token_hex(8)
                self._metrics_call("record_action", action_id, observation_id=context.get("observation_id"),
                                   kind=context.get("kind", "unknown"), attempt=context.get("attempt", 1),
                                   retry_of=context.get("retry_of"))
                self._metrics_call("record_outcome", action_id, result.outcome, verdict=result.verdict)
                result = replace(result, metrics_action_id=action_id)
            return result
        finally:
            self._metrics_context.value = {}

    # --- actions ----------------------------------------------------------

    def submit_click(
        self, *, observation_id: str, x: float, y: float,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE,
        button: str = "left", rebind: bool = False, anchor: dict | None = None,
    ) -> Dispatch:
        """Click at a coordinate in an image the server issued.

        The caller names the observation it was given; the provenance the
        arbiter checks comes from the record made at issue time, never from the
        caller and never re-read from the present.

        ``button`` exists because a left click cannot craft. Minecraft's
        inventory splits a stack with the right button -- left places all of it,
        right places one -- so an agent restricted to left clicks can carry
        items around a grid but cannot put one plank in each of four slots,
        which is the first recipe in the game. `Arbiter.click_action` already
        took a button; nothing could reach it.
        """
        if anchor is not None:
            anchor = parse_anchor(anchor, x, y)
            if not rebind:
                raise ValueError("anchor requires rebind=true")
        bound = self._bind(observation_id, label, rebind, points=[(x, y)], anchor=anchor)
        if isinstance(bound, Dispatch):
            return bound
        observation, binding = bound

        try:
            action = self.arbiter.click_action(
                observation, x, y, label=label, source=source,
                button=_button(button),
            )
        except ActionRejected as exc:
            # Report the reason the arbiter actually gave. Collapsing every
            # refusal into GEOMETRY_MOVED made an out-of-bounds coordinate look
            # like a window that had moved, which sends anyone debugging it
            # looking in the wrong place.
            entry = self.log.add(f"{label}: {exc}", "denied")
            self.log.mark(x, y, "denied", label, entry_id=entry)
            return Dispatch(exc.reason.name, "denied", str(exc), binding=binding)
        except Exception as exc:
            self.log.add(f"{label}: unexpected failure: {exc}", "error")
            log.exception("submit_click failed for %r", label)
            return Dispatch("ERROR", "error", str(exc), binding=binding)

        return self._dispatch(action, x, y, label, wait=wait, measure=measure,
                              settle=settle, binding=binding)

    def submit_observation_click(
        self, observation, x: float, y: float, *, label: str = "",
        source: str = "agent", wait: float = 0.0,
        measure: bool = False, settle: float = CHURN_SETTLE,
    ) -> Dispatch:
        """In-process path: the caller already holds the observation it reasoned about.

        Same rules, no registry round trip -- an agent inside this process never
        had to be handed an id to know what it was looking at. ``wait`` defaults
        to zero here: an agent tier submitting from its own loop must not block
        on the input queue, and it learns the outcome from the next frame.

        ``measure`` and ``settle`` are declared rather than inherited: this
        method forwarded both to ``_dispatch`` without ever taking them, so
        every successfully built click raised NameError before anything was
        dispatched. It reached the reflex loop (agent.py) as a logged failure
        and the vision tier as a dead worker, and no test crossed this path --
        the HTTP surface uses ``submit_click``, which does take them.
        """
        try:
            action = self.arbiter.click_action(
                observation, x, y, label=label, source=source
            )
        except ActionRejected as exc:
            entry = self.log.add(f"{label}: {exc}", "denied")
            self.log.mark(x, y, "denied", label, entry_id=entry)
            return Dispatch(exc.reason.name, "denied", str(exc))
        except Exception as exc:
            self.log.add(f"{label}: unexpected failure: {exc}", "error")
            log.exception("submit_observation_click failed for %r", label)
            return Dispatch("ERROR", "error", str(exc))
        return self._dispatch(action, x, y, label, wait=wait, measure=measure, settle=settle)

    def submit_key(
        self, *, observation_id: str, key: str, hold: float = 0.08,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE, rebind: bool = False,
    ) -> Dispatch:
        """Press one key in the image's world. See Arbiter.key_action."""
        return self._submit_named(
            observation_id, label or f"key {key}", source, wait, measure, settle, rebind,
            lambda obs: self.arbiter.key_action(
                obs, key, hold=hold, label=label or "key", source=source
            ),
        )

    def submit_press(
        self, *, observation_id: str, button: str = "left", hold: float = 0.08,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE, rebind: bool = False,
    ) -> Dispatch:
        """Hold a mouse button without moving. See Arbiter.press_action."""
        return self._submit_named(
            observation_id, label or f"press {button}", source, wait, measure, settle, rebind,
            lambda obs: self.arbiter.press_action(
                obs, button=button, hold=hold, label=label or "press", source=source
            ),
        )

    def submit_look(
        self, *, observation_id: str, dx: float, dy: float,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE, rebind: bool = False,
    ) -> Dispatch:
        """Turn the camera. See Arbiter.look_action."""
        return self._submit_named(
            observation_id, label or f"look {dx:+.0f},{dy:+.0f}", source, wait, measure, settle,
            rebind,
            lambda obs: self.arbiter.look_action(
                obs, dx, dy, label=label or "look", source=source
            ),
        )

    def submit_sequence(
        self, *, observation_id: str, steps: list,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE, rebind: bool = False,
    ) -> Dispatch:
        """Several overlapping primitives as one decision. ``steps`` must already
        have been through ``arbiter.parse_sequence``; see Arbiter.sequence_action.

        Every point the sequence moves the pointer to is a click point for
        rebinding: each must still look as it was shown."""
        from gamelens.arbiter import sequence_points

        return self._submit_named(
            observation_id, label or "sequence", source, wait, measure, settle, rebind,
            lambda obs: self.arbiter.sequence_action(
                obs, steps, label=label or "sequence", source=source
            ),
            points=sequence_points(steps),
        )

    def submit_scroll(
        self, *, observation_id: str, clicks: float, horizontal: bool = False,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE, rebind: bool = False,
    ) -> Dispatch:
        """Turn the mouse wheel. See Arbiter.scroll_action."""
        return self._submit_named(
            observation_id, label or "scroll", source, wait, measure, settle, rebind,
            lambda obs: self.arbiter.scroll_action(
                obs, clicks, horizontal=horizontal, label=label or "scroll", source=source
            ),
        )

    def sequence_capacity(self) -> float:
        """How many new inputs one sequence may contain: the rate bucket's size."""
        return self.safety.rate_capacity

    def _bind(self, observation_id: str, label: str, rebind: bool, *, points=(), anchor=None):
        """The observation an action runs on, and how it was chosen.

        Returns ``(observation, binding)`` or a denial ``Dispatch``.

        Without ``rebind`` this is the record the caller names, as it always
        was. With it, an observation that has aged past the arbiter's limits is
        carried over to the newest frame -- the only way an agent whose own turn
        outlasts ACTION_TTL can act at all -- but only when age is all that
        changed (GL039-R2): target, capture session, geometry, size and the
        preemption counter are compared between the two records, not merely
        against the present. For a click -- and for every point a sequence
        moves to -- the pixels there must also still be what the caller was
        shown (GL039-R3); the worst point is what gets reported. A record that is
        gone fails closed: there is nothing left to compare against.
        """
        record = self.observations.record(observation_id)
        if record is None:
            detail = f"observation {observation_id[:8]}... is unknown or expired"
            self.log.add(f"{label}: {detail}", "denied")
            return Dispatch(Rejection.STALE_OBSERVATION.name, "denied", detail)
        shown, shown_jpeg, quality, moving = record
        if not rebind:
            return shown, None
        if self.arbiter.is_fresh(shown) and anchor is None:
            return shown, {"bound_to": "shown"}

        binding: dict = {"bound_to": None, "shown_frame": shown.frame_id}
        verdict, detail = Rejection.OK, ""
        frame = self.capture.frames.acquire(timeout=0)
        if frame is None:
            verdict = Rejection.NO_FRAME
        else:
            try:
                fresh = self.arbiter.observation_for(
                    frame, scale=shown.scale, crop=(shown.crop_left, shown.crop_top))
                binding["frame"] = fresh.frame_id
                verdict = self.arbiter.rebind_rejection(shown, fresh)
                if verdict is Rejection.OK and points:
                    if shown_jpeg is None:
                        verdict = Rejection.STALE_OBSERVATION
                        detail = "no image on record to compare the click point against"
                    else:
                        fresh_jpeg, scale = encode_jpeg(frame.array, quality=quality or 70)
                        if anchor is not None:
                            same = same_at_anchor(shown_jpeg, fresh_jpeg, anchor, *points[0])
                            binding.update(same)
                            if scale != shown.scale:
                                verdict = Rejection.GEOMETRY_MOVED
                            elif not same["anchor_ok"]:
                                detail = "the visible yellow anchor label changed; look again"
                                self.log.add(f"{label}: {detail}", "denied")
                                return Dispatch("TARGET_CHANGED", "denied", detail, binding=binding)
                            else:
                                binding["bound_to"] = "anchored"
                                return fresh, binding
                        checks = [same_at_click(shown_jpeg, fresh_jpeg, *p, moving=moving)
                                  for p in points]
                        # The worst point speaks for all of them. A point that
                        # could not be compared at all (None scores) is worst.
                        same = next((c for c in checks if c["patch_max"] is None), None)                             or max(checks, key=lambda c: (c["patch_max"], c["patch_mean"]))
                        same = dict(same, ok=all(c["ok"] for c in checks))
                        binding.update({k: v for k, v in same.items() if k != "ok"})
                        if len(points) > 1:
                            binding["points"] = len(points)
                        if scale != shown.scale:
                            verdict = Rejection.GEOMETRY_MOVED
                        elif not same["ok"]:
                            detail = ("the screen at the click point changed since it was "
                                      "shown; look again")
                            self.log.add(f"{label}: {detail}", "denied")
                            return Dispatch("SCREEN_CHANGED", "denied", detail, binding=binding)
            except Exception as exc:
                log.exception("rebind failed for %r", label)
                return Dispatch("ERROR", "error", str(exc), binding=binding)
            finally:
                frame.release()
        if verdict is not Rejection.OK:
            detail = detail or f"{verdict.value}; look again"
            self.log.add(f"{label}: not rebound: {detail}", "denied")
            return Dispatch(verdict.name, "denied", detail, binding=binding)
        binding["bound_to"] = "fresh"
        return fresh, binding

    def _submit_named(self, observation_id, label, source, wait, measure, settle, rebind,
                      build, *, points=()) -> Dispatch:
        """Shared path for actions that have no coordinate to draw or check.

        A keystroke and a camera turn carry the same provenance rules as a
        click -- the observation must still be the world that justified them --
        but there is nothing to put on the overlay, so they get a log entry and
        no mark.
        """
        bound = self._bind(observation_id, label, rebind, points=points)
        if isinstance(bound, Dispatch):
            return bound
        observation, binding = bound
        try:
            action = build(observation)
        except ActionRejected as exc:
            self.log.add(f"{label}: {exc}", "denied")
            return Dispatch(exc.reason.name, "denied", str(exc), binding=binding)
        except ValueError as exc:
            # An unknown key name. A caller error, not a world that moved.
            self.log.add(f"{label}: {exc}", "denied")
            return Dispatch("BAD_REQUEST", "denied", str(exc), binding=binding)
        except Exception as exc:
            self.log.add(f"{label}: unexpected failure: {exc}", "error")
            log.exception("%s failed", label)
            return Dispatch("ERROR", "error", str(exc), binding=binding)
        return self._dispatch(action, None, None, label, wait=wait, measure=measure,
                              settle=settle, binding=binding)

    def _dispatch(
        self, action, x: float | None, y: float | None, label: str,
        *, wait: float = 0.0, measure: bool = False,
        settle: float = CHURN_SETTLE, binding: dict | None = None,
    ) -> Dispatch:
        """Submit an action and, optionally, wait to find out what became of it.

        Two separate facts, and the old code reported only the first as if it
        were both. ``verdict`` is the arbiter's: the world was still the one the
        action was reasoned about. ``outcome`` is the executor's: the press
        happened, or was refused at press time by an interlock that gets its own
        vote after the queue and the dwell.

        The wait exists because the gap between them is where a whole debugging
        session goes. It is bounded and small -- a click is a move, a settle and
        a hold -- and a caller that times out is told ``pending`` rather than a
        guess.
        """
        where = f" at ({int(x)},{int(y)})" if x is not None and y is not None else ""
        entry_id = self.log.add(f"{label}{where}", "queued")
        context = getattr(getattr(self, "_metrics_context", None), "value", {})
        bound_observation_id = "action-source-" + str(action.action_id)
        metrics_observation = context.get("observation_id") or bound_observation_id
        self._metrics_call("record_observation", bound_observation_id, action.observation, source="action", freshness_limit=getattr(self.arbiter, "_observation_deadline", 1.0))
        self._metrics_call("record_action", action.action_id, observation_id=metrics_observation,
                           kind=context.get("kind", "guarded"), attempt=context.get("attempt", 1),
                           retry_of=context.get("retry_of"), log_id=entry_id, bound_observation_id=bound_observation_id)
        if x is not None and y is not None:
            self.log.mark(x, y, "queued", label, entry_id=entry_id)

        # Sampled before submission and again after the outcome settles. Only
        # when the caller is already waiting: taking an "after" the action has
        # not finished would compare the screen to itself and call every action
        # ineffective.
        try:
            before = self._thumbnail() if measure else None
        except Exception:
            # An action must never fail because the thing watching it did. This
            # is a measurement bolted onto the dispatch path, and the dispatch
            # path is the one that presses buttons in somebody's game.
            log.debug("churn baseline failed", exc_info=True)
            before = None

        settled = threading.Event()
        box: list = []
        marks: list = []

        def on_outcome(outcome) -> None:
            # Read before anything else on this path: the point is the frame
            # that was newest when injection ended, not a moment later.
            try:
                marks.append(self.capture.frames.latest_id())
            except Exception:
                log.debug("after_frame read failed", exc_info=True)
            box.append(outcome)
            self.log.resolve(entry_id, outcome.status, outcome.detail)
            settled.set()
            self._metrics_call("record_outcome", action.action_id, outcome.status, verdict="ok",
                               completed_steps=getattr(outcome, "completed_steps", None),
                               injected_steps=getattr(outcome, "injected_steps", None),
                               partial=getattr(outcome, "partial", False),
                               last_completed_step=getattr(outcome, "last_completed_step", None),
                               after_frame=marks[0] if marks and (outcome.status == "sent" or getattr(outcome, "injected_steps", 0) > 0) else None)

        verdict = self.arbiter.submit(action, on_outcome=on_outcome)
        if verdict is not Rejection.OK:
            # Never queued, so no outcome is coming; resolve it here or the
            # entry sits at "queued" forever.
            self.log.resolve(entry_id, "denied", verdict.value)
            self._metrics_call("record_outcome", action.action_id, "denied", verdict=verdict.name)
            return Dispatch(verdict.name, "denied", verdict.value, action.action_id,
                            binding=binding)

        if wait > 0:
            # The budget has to cover the action's own length, not just the
            # queue. A click is ~200ms and fits; a key held for two seconds
            # never would, so every long press would report "pending" while
            # working perfectly -- the honest answer to the wrong question.
            settled.wait(wait + _expected_duration(action))
        outcome = box[0] if box else None
        if outcome is None:
            self._metrics_call("record_outcome", action.action_id, "pending", verdict="ok")
        # 0 means nothing had been published yet: there is no frame to wait
        # past, and reporting 0 would make "any frame at all" look like "after".
        injected = (outcome is not None and (outcome.status == "sent" or getattr(outcome, "injected_steps", 0) > 0)
                    and bool(marks) and marks[0] > 0)
        return Dispatch(
            "ok",
            outcome.status if outcome else "pending",
            outcome.detail if outcome else "",
            action.action_id,
            completed_steps=getattr(outcome, "completed_steps", None),
            injected_steps=getattr(outcome, "injected_steps", None),
            last_completed_step=getattr(outcome, "last_completed_step", None),
            partial=getattr(outcome, "partial", False),
            churn=self._churn_since(before, outcome, settle),
            after_frame=marks[0] if injected else None,
            binding=binding,
        )

    def _churn_since(self, before, outcome, settle: float) -> float | None:
        """How much the screen moved since ``before``, or None if unmeasurable.

        This answers "did anything change", which is the question GL-036 was
        opened for: an action can be injected perfectly and accomplish nothing,
        and until now the two were indistinguishable from outside -- a mining
        hold shorter than the block's own break time returned ``sent`` a hundred
        and sixty times without breaking a single block.

        It is evidence, not proof, and in both directions. Rain, a passing mob
        or a sunrise move pixels on their own, so a high number does not mean
        the action worked; and walking into a wall changes nothing on screen,
        so a low one does not mean it failed.

        The sampling straddles the action rather than spanning it, which is the
        whole reason it can say anything at all. Measured across the action, a
        mining hold reports motion either way, because the game animates cracks
        on the block for as long as the button is down; the first version of
        this measurement did exactly that and could not have caught the defect
        it was written for. Measured from before the action to after the screen
        has settled, the transient is gone and what remains is what actually
        changed.
        """
        if before is None or outcome is None or outcome.status != "sent":
            # Nothing was injected, so the screen is not evidence about it.
            return None
        try:
            # Clamped, and inside the guard. Both matter once the duration can
            # come from a request: time.sleep() raises on a negative or
            # non-finite argument, and this runs *after* the input was injected
            # -- so an unguarded raise here would turn a keystroke the game has
            # already received into an HTTP 500, and a caller that retries would
            # send it twice. Instrumentation does not get to fail the action.
            # OverflowError belongs here with the others: float(10**400) does
            # not return inf, it raises -- and this runs after the key is in
            # the game, where a raise is a 500 for an action that happened.
            settle = min(MAX_SETTLE, max(0.0, float(settle)))
        except (TypeError, ValueError, OverflowError):
            settle = CHURN_SETTLE
        if settle != settle:                      # NaN survives the comparisons
            settle = CHURN_SETTLE
        try:
            time.sleep(settle)
            after = self._thumbnail()
        except Exception:
            log.debug("churn measurement failed", exc_info=True)
            return None
        if after is None or after.shape != before.shape:
            return None
        return round(float(np.abs(after - before).mean()), 3)

    def attach_agent(self, agent) -> None:
        self.agent = agent

    def set_intent(self, text: str) -> None:
        self._agent_intent = text

    # --- state for the dashboard -------------------------------------------

    def _fps(self, capture: dict) -> float:
        """The capture backend's own publication rate.

        This used to be derived from `self._frame_times`, which `_watch` fills
        at most once per 50ms iteration -- so the answer could never exceed 20
        no matter how fast capture ran, and reported 16.0 for a backend
        publishing 48.7 frames a second. The number now comes from the
        publisher; the poller keeps its other jobs.
        """
        return round(float(capture.get("publish_rate", 0.0)), 1)

    def state(self) -> dict:
        capture = self.capture.stats()
        ages = list(self._frame_ages)
        p95 = (
            statistics.quantiles(ages, n=20)[-1]
            if len(ages) >= 20 else (max(ages) if ages else 0.0)
        )

        try:
            current = describe(self.target.hwnd)
            target = {
                "title": current.title,
                "hwnd": current.hwnd,
                "width": current.width,
                "height": current.height,
                "foreground": current.foreground,
            }
        except Exception:
            target = None

        agent_stats = self.agent.stats() if self.agent else {}
        safety = {**self.safety.snapshot(), "executor": self.executor.snapshot()}
        session = session_feedback(target, capture, safety, self.safety.check(consume=False).value)
        entries = self.log.entries()
        session["latest_event"] = entries[-1] if entries else None
        return {
            "target": target,
            "capture": {
                "backend": capture["backend"],
                # Which capture session produced the frames, and whether
                # failover is off: a caller scoring frames across an interval
                # needs both to know every frame came from one source (GL039-R7).
                "session_id": capture.get("session_id"),
                "forced_backend": capture.get("forced_backend"),
                "fps": self._fps(capture),
                "age_ms": capture["age_ms"] if capture["age_ms"] != float("inf") else 9999,
                "p95_age_ms": p95,
                "frame_id": capture["frame_id"],
                "width": capture["width"],
                "height": capture["height"],
                "dropped": capture["pool"]["exhausted"],
                "duplicates": capture["duplicates"],
                "healthy": capture["healthy"],
                "transitions": capture["transitions"][-4:],
            },
            "safety": safety,
            "session": session,
            "capabilities": {"anchored_click": "yellow-label-v1", "shared_input": True,
                             "cropped_perception": "full-parent-v1", "run_metrics": "run-v1",
                             "reusable_skills": "guarded-v1", "reviewed_learning": "owner-review-v1"},
            "run": {"id": getattr(self, "run_id", None), "persistence_enabled":
                    bool(getattr(getattr(self, "run_store", None), "enabled", False))},
            "recording": self.recorder.snapshot() if hasattr(self, "recorder") else None,
            "observations": {"retention_seconds": self.observations.retention_seconds,
                             "snapshot_capacity": self.observations._capacity,
                             "stream_retention_seconds": 5.0},
            "agent": {
                "tier": agent_stats.get("tier", "off"),
                "reflex_p95_ms": agent_stats.get("reflex_p95_ms", 0.0),
                "vision_fps": agent_stats.get("vision_fps", 0.0),
                "intent": agent_stats.get("intent", self._agent_intent),
            },
            "arbiter": self.arbiter.stats(),
            "marks": self.log.marks(),
            "log": entries,
        }
