"""The runtime: one object that owns every part and wires them together."""

from __future__ import annotations

import logging
import secrets
import statistics
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

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

    @property
    def ok(self) -> bool:
        """True only when the action was accepted *and* not refused afterwards."""
        return self.verdict == "ok" and self.outcome in ("sent", "dry", "pending")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "outcome": self.outcome,
            "detail": self.detail,
            "action_id": self.action_id,
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

        return self._dispatch(action, x, y, label, wait=wait)

    def submit_observation_click(
        self, observation, x: float, y: float, *, label: str = "",
        source: str = "agent", wait: float = 0.0,
    ) -> Dispatch:
        """In-process path: the caller already holds the observation it reasoned about.

        Same rules, no registry round trip -- an agent inside this process never
        had to be handed an id to know what it was looking at. ``wait`` defaults
        to zero here: an agent tier submitting from its own loop must not block
        on the input queue, and it learns the outcome from the next frame.
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
        return self._dispatch(action, x, y, label, wait=wait)

    def _dispatch(
        self, action, x: float, y: float, label: str, *, wait: float = 0.0
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
        entry_id = self.log.add(f"{label} at ({int(x)},{int(y)})", "queued")
        self.log.mark(x, y, "queued", label, entry_id=entry_id)

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
            settled.wait(wait)
        outcome = box[0] if box else None
        return Dispatch(
            "ok",
            outcome.status if outcome else "pending",
            outcome.detail if outcome else "",
            action.action_id,
        )

    def attach_agent(self, agent) -> None:
        self.agent = agent

    def set_intent(self, text: str) -> None:
        self._agent_intent = text

    # --- state for the dashboard -------------------------------------------

    def _fps(self) -> float:
        times = list(self._frame_times)
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

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
                "fps": self._fps(),
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
