"""The runtime: one object that owns every part and wires them together."""

from __future__ import annotations

import logging
import secrets
import statistics
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

import cv2
import numpy as np

from gamelens.arbiter import ActionRejected, Arbiter, Rejection
from gamelens.capture import Backend, CaptureSupervisor
from gamelens.coords import GeometryTracker
from gamelens.input import InputExecutor
from gamelens.safety import SafetySupervisor
from gamelens.server import ActionLog, Tokens, encode_jpeg
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
            "churn": self.churn,
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

    def __init__(self, capacity: int = 256, ttl: float = 5.0) -> None:
        self._records: OrderedDict = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl
        self._lock = threading.Lock()

    def issue(self, observation) -> str:
        token = secrets.token_urlsafe(9)
        now = time.monotonic()
        with self._lock:
            self._records[token] = (observation, now)
            while len(self._records) > self._capacity:
                self._records.popitem(last=False)
            # Opportunistic expiry; bounded work per issue.
            for key in [k for k, (_, t) in list(self._records.items())[:8]
                        if now - t > self._ttl]:
                self._records.pop(key, None)
        return token

    def resolve(self, token: str):
        with self._lock:
            entry = self._records.get(token)
            if entry is None:
                return None
            observation, issued_at = entry
            if time.monotonic() - issued_at > self._ttl:
                self._records.pop(token, None)
                return None
            return observation


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
    ) -> None:
        self.target: WindowInfo = (
            target if isinstance(target, WindowInfo) else find_window(target)
        )
        self.port = port
        self.tokens = Tokens()
        self.log = ActionLog()

        self.capture = CaptureSupervisor(
            self.target, pool_depth=pool_depth, forced=backend
        )
        self.geometry = GeometryTracker(self.target.hwnd)
        self.safety = SafetySupervisor(self.target.hwnd, rate=rate)
        self.executor = InputExecutor(self.safety)
        self.arbiter = Arbiter(self.capture, self.geometry, self.executor)

        self._frame_times: deque = deque(maxlen=120)
        self._frame_ages: deque = deque(maxlen=120)
        self._last_frame_id = 0
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

        self.observations = ObservationRegistry()

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
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=1.0)
        if self.agent:
            self.agent.stop()
        self.executor.shutdown()
        self.capture.stop()
        self.safety.shutdown()

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

    def encode_latest(self, quality: int = 70) -> tuple[bytes | None, str]:
        """Encode the newest frame and register what was handed out.

        Returns ``(jpeg, observation_id)``. The record captures the transport
        scale, so a coordinate in the delivered image can be mapped back to the
        native frame -- without it, a 1920-wide frame served at 1280 would put
        every click two thirds of the way to where it belonged.
        """
        frame = self.capture.frames.acquire()
        if frame is None:
            return None, ""
        try:
            jpeg, scale = encode_jpeg(frame.array, quality=quality)
            observation = self.arbiter.observation_for(frame, scale=scale)
            return jpeg, self.observations.issue(observation)
        except Exception:
            log.exception("frame encode failed")
            return None, ""
        finally:
            frame.release()

    # --- actions ----------------------------------------------------------

    def submit_click(
        self, *, observation_id: str, x: float, y: float,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE,
    ) -> Dispatch:
        """Click at a coordinate in an image the server issued.

        The caller names the observation it was given; the provenance the
        arbiter checks comes from the record made at issue time, never from the
        caller and never re-read from the present.
        """
        observation = self.observations.resolve(observation_id)
        if observation is None:
            detail = f"observation {observation_id[:8]}... is unknown or expired"
            self.log.add(f"{label}: {detail}", "denied")
            return Dispatch(Rejection.STALE_OBSERVATION.name, "denied", detail)

        try:
            action = self.arbiter.click_action(
                observation, x, y, label=label, source=source
            )
        except ActionRejected as exc:
            # Report the reason the arbiter actually gave. Collapsing every
            # refusal into GEOMETRY_MOVED made an out-of-bounds coordinate look
            # like a window that had moved, which sends anyone debugging it
            # looking in the wrong place.
            entry = self.log.add(f"{label}: {exc}", "denied")
            self.log.mark(x, y, "denied", label, entry_id=entry)
            return Dispatch(exc.reason.name, "denied", str(exc))
        except Exception as exc:
            self.log.add(f"{label}: unexpected failure: {exc}", "error")
            log.exception("submit_click failed for %r", label)
            return Dispatch("ERROR", "error", str(exc))

        return self._dispatch(action, x, y, label, wait=wait, measure=measure, settle=settle)

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
        measure: bool = False, settle: float = CHURN_SETTLE,
    ) -> Dispatch:
        """Press one key in the image's world. See Arbiter.key_action."""
        return self._submit_named(
            observation_id, label or f"key {key}", source, wait, measure, settle,
            lambda obs: self.arbiter.key_action(
                obs, key, hold=hold, label=label or "key", source=source
            ),
        )

    def submit_press(
        self, *, observation_id: str, button: str = "left", hold: float = 0.08,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE,
    ) -> Dispatch:
        """Hold a mouse button without moving. See Arbiter.press_action."""
        return self._submit_named(
            observation_id, label or f"press {button}", source, wait, measure, settle,
            lambda obs: self.arbiter.press_action(
                obs, button=button, hold=hold, label=label or "press", source=source
            ),
        )

    def submit_look(
        self, *, observation_id: str, dx: float, dy: float,
        label: str = "", source: str = "agent", wait: float = DISPATCH_WAIT,
        measure: bool = False, settle: float = CHURN_SETTLE,
    ) -> Dispatch:
        """Turn the camera. See Arbiter.look_action."""
        return self._submit_named(
            observation_id, label or f"look {dx:+.0f},{dy:+.0f}", source, wait, measure, settle,
            lambda obs: self.arbiter.look_action(
                obs, dx, dy, label=label or "look", source=source
            ),
        )

    def _submit_named(self, observation_id, label, source, wait, measure, settle, build) -> Dispatch:
        """Shared path for actions that have no coordinate to draw or check.

        A keystroke and a camera turn carry the same provenance rules as a
        click -- the observation must still be the world that justified them --
        but there is nothing to put on the overlay, so they get a log entry and
        no mark.
        """
        observation = self.observations.resolve(observation_id)
        if observation is None:
            detail = f"observation {observation_id[:8]}... is unknown or expired"
            self.log.add(f"{label}: {detail}", "denied")
            return Dispatch(Rejection.STALE_OBSERVATION.name, "denied", detail)
        try:
            action = build(observation)
        except ActionRejected as exc:
            self.log.add(f"{label}: {exc}", "denied")
            return Dispatch(exc.reason.name, "denied", str(exc))
        except ValueError as exc:
            # An unknown key name. A caller error, not a world that moved.
            self.log.add(f"{label}: {exc}", "denied")
            return Dispatch("BAD_REQUEST", "denied", str(exc))
        except Exception as exc:
            self.log.add(f"{label}: unexpected failure: {exc}", "error")
            log.exception("%s failed", label)
            return Dispatch("ERROR", "error", str(exc))
        return self._dispatch(action, None, None, label, wait=wait, measure=measure, settle=settle)

    def _dispatch(
        self, action, x: float | None, y: float | None, label: str,
        *, wait: float = 0.0, measure: bool = False,
        settle: float = CHURN_SETTLE,
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

        def on_outcome(outcome) -> None:
            box.append(outcome)
            self.log.resolve(entry_id, outcome.status, outcome.detail)
            settled.set()

        verdict = self.arbiter.submit(action, on_outcome=on_outcome)
        if verdict is not Rejection.OK:
            # Never queued, so no outcome is coming; resolve it here or the
            # entry sits at "queued" forever.
            self.log.resolve(entry_id, "denied", verdict.value)
            return Dispatch(verdict.name, "denied", verdict.value, action.action_id)

        if wait > 0:
            # The budget has to cover the action's own length, not just the
            # queue. A click is ~200ms and fits; a key held for two seconds
            # never would, so every long press would report "pending" while
            # working perfectly -- the honest answer to the wrong question.
            settled.wait(wait + _expected_duration(action))
        outcome = box[0] if box else None
        return Dispatch(
            "ok",
            outcome.status if outcome else "pending",
            outcome.detail if outcome else "",
            action.action_id,
            churn=self._churn_since(before, outcome, settle),
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
        return {
            "target": target,
            "capture": {
                "backend": capture["backend"],
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
            "safety": {
                **self.safety.snapshot(),
                "executor": self.executor.snapshot(),
            },
            "agent": {
                "tier": agent_stats.get("tier", "off"),
                "reflex_p95_ms": agent_stats.get("reflex_p95_ms", 0.0),
                "vision_fps": agent_stats.get("vision_fps", 0.0),
                "intent": agent_stats.get("intent", self._agent_intent),
            },
            "arbiter": self.arbiter.stats(),
            "marks": self.log.marks(),
            "log": self.log.entries(),
        }
