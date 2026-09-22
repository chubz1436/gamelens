"""The single gate every action passes through.

An agent decides what to do by looking at a picture. By the time the decision
comes back, the world may have moved: the window may have been dragged, the
capture may have stalled, a faster reflex may already have acted, or the whole
capture backend may have been replaced. Mapping a stale decision through current
geometry is how a click lands somewhere nobody intended.

So actions are not bare coordinates. Each one carries the provenance of the
observation it came from, captured *before* the thinking started, and the
arbiter refuses anything whose world has moved on.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)

_action_ids = itertools.count(1)

# How old the *source frame* may be when an action derived from it is executed.
OBSERVATION_DEADLINE = 1.0
# How long an action may sit between being created and being executed.
ACTION_TTL = 0.8

# Ceilings on the two things a caller can ask for that have no coordinate and
# therefore no bounds check to fall back on. Both are clamps, not validations:
# the action still happens, just not to an absurd degree.
MAX_KEY_HOLD = 2.0          # seconds a key may be held by one action
MAX_LOOK_DELTA = 600        # mickeys per look action, per axis
# Longer than a key hold on purpose: breaking a block is a sustained press of
# several seconds, and capping it at a key's ceiling would make mining
# impossible rather than merely awkward.
MAX_BUTTON_HOLD = 5.0       # seconds a mouse button may be held by one action


class Rejection(Enum):
    OK = "ok"
    WRONG_TARGET = "action targets a different window"
    RETIRED_SESSION = "capture backend was replaced after this was observed"
    STALE_OBSERVATION = "source frame was too old to act on"
    GEOMETRY_MOVED = "window moved or resized after this was observed"
    EXPIRED = "action exceeded its time to live"
    PREEMPTED = "a faster tier acted after this was observed"
    NO_FRAME = "no frame is available"
    OUT_OF_BOUNDS = "coordinate falls outside the target window"


class ActionRejected(RuntimeError):
    def __init__(self, reason: Rejection, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason.value}{(': ' + detail) if detail else ''}")


@dataclass(frozen=True)
class Observation:
    """What was true when we *started* looking, not when we finished.

    Every field here is snapshotted before inference begins and propagated
    unchanged. Re-stamping any of it on completion would reset the clock the
    freshness checks depend on and hide a preemption that happened while the
    model was thinking -- which is precisely the window this guards.
    """

    target_hwnd: int
    backend_session_id: int
    frame_id: int
    frame_captured_at: float
    frame_width: int
    frame_height: int
    geometry_generation: int
    preemption_counter: int
    # How the image handed to the model relates to the frame: the model sees a
    # downscaled crop, so its coordinates mean nothing without this.
    scale: float = 1.0
    crop_left: int = 0
    crop_top: int = 0
    observed_at: float = field(default_factory=time.monotonic)

    def to_frame_xy(self, x: float, y: float) -> tuple[float, float]:
        """Map a coordinate in the model's image back to a frame pixel."""
        return self.crop_left + x / self.scale, self.crop_top + y / self.scale

    def age(self) -> float:
        return time.monotonic() - self.frame_captured_at


@dataclass
class Action:
    """A proposed input, bound to the observation that justifies it."""

    observation: Observation
    steps: list
    label: str = ""
    source: str = "agent"
    action_id: int = field(default_factory=lambda: next(_action_ids))
    created_at: float = field(default_factory=time.monotonic)

    def age(self) -> float:
        return time.monotonic() - self.created_at


class Arbiter:
    """Validates actions against the world as it is *now*.

    Both agent tiers and the HTTP endpoint go through this one object. Having
    two paths would mean two sets of rules, and the weaker one would decide what
    the system actually does.
    """

    def __init__(
        self,
        capture,
        geometry_tracker,
        executor,
        *,
        observation_deadline: float = OBSERVATION_DEADLINE,
        action_ttl: float = ACTION_TTL,
    ) -> None:
        self._capture = capture
        self._geometry = geometry_tracker
        self._executor = executor
        self._observation_deadline = observation_deadline
        self._action_ttl = action_ttl

        self._lock = threading.Lock()
        self._preemptions = 0
        self.accepted = 0
        self.rejected = 0
        self.rejections: dict[str, int] = {}

    # --- observing ------------------------------------------------------

    @property
    def preemption_counter(self) -> int:
        with self._lock:
            return self._preemptions

    def preempt(self, why: str = "") -> int:
        """Invalidate every observation taken before now.

        Called by the fast tier when it acts. A slow-tier decision still in
        flight was made about a world that has since been changed by the reflex,
        so it must not be executed on arrival.
        """
        with self._lock:
            self._preemptions += 1
            counter = self._preemptions
        log.debug("preemption %d%s", counter, f" ({why})" if why else "")
        return counter

    def observe(self, *, scale: float = 1.0, crop: tuple[int, int] = (0, 0)):
        """Take a frame plus the provenance needed to act on it later.

        Returns ``(frame, observation)``. **The caller owns the frame lease and
        must release it** -- a dropped lease starves the buffer pool, and once it
        is empty capture silently drops every frame. Callers that already hold a
        frame should use ``observation_for`` instead of acquiring a second one.
        """
        frame = self._capture.frames.acquire()
        if frame is None:
            raise ActionRejected(Rejection.NO_FRAME)
        return frame, self.observation_for(frame, scale=scale, crop=crop)

    def observation_for(
        self, frame, *, scale: float = 1.0, crop: tuple[int, int] = (0, 0)
    ) -> Observation:
        """Build provenance for a frame the caller already holds.

        Separate from ``observe`` so a caller holding a lease never has to take a
        second one -- which would both leak the lease and describe a *different*
        frame than the one it checked.
        """
        geometry = self._geometry.current
        with self._lock:
            preemptions = self._preemptions

        return Observation(
            target_hwnd=self._capture.binding.hwnd,
            backend_session_id=frame.session_id,
            frame_id=frame.frame_id,
            frame_captured_at=frame.captured_at,
            frame_width=frame.width,
            frame_height=frame.height,
            geometry_generation=geometry.generation,
            preemption_counter=preemptions,
            scale=scale,
            crop_left=crop[0],
            crop_top=crop[1],
        )

    # --- validating -----------------------------------------------------

    def evaluate(self, action: Action) -> Rejection:
        obs = action.observation

        if obs.target_hwnd != self._capture.binding.hwnd:
            return Rejection.WRONG_TARGET

        backend = self._capture.backend
        if backend is None or backend.session_id != obs.backend_session_id:
            # The capture method changed underneath this observation. Frame ids
            # and geometry from the old session say nothing about the new one.
            return Rejection.RETIRED_SESSION
        if getattr(backend, "retired", False):
            # Matching ids are not enough: a backend can be retired and still be
            # the one on record for the moment it takes to swap it out.
            return Rejection.RETIRED_SESSION

        # The check the geometry rules cannot make. A stalled capture leaves an
        # old frame in place: with the game still foreground and the window
        # unmoved, an action built from it has a fresh created_at and an
        # unchanged generation, and would otherwise pass everything while
        # describing a scene that is seconds out of date.
        if obs.age() > self._observation_deadline:
            return Rejection.STALE_OBSERVATION

        if self._geometry.generation != obs.geometry_generation:
            return Rejection.GEOMETRY_MOVED

        # Measured from when the observation was taken, not from when the
        # action was built. Building it after a 900ms inference would otherwise
        # hand a stale decision a fresh 800ms budget.
        if (time.monotonic() - obs.observed_at) > self._action_ttl:
            return Rejection.EXPIRED
        if action.age() > self._action_ttl:
            return Rejection.EXPIRED

        with self._lock:
            if self._preemptions != obs.preemption_counter:
                return Rejection.PREEMPTED

        return Rejection.OK

    def submit(self, action: Action, *, on_outcome=None) -> Rejection:
        """Validate and, if it survives, hand the action to the executor.

        The returned verdict is about *acceptance*, which is not the same claim
        as "this happened". ``on_outcome`` is how a caller learns the second
        thing: the executor calls it exactly once with an
        :class:`~gamelens.input.Outcome` when the action is finally decided.
        """
        verdict = self.evaluate(action)
        if verdict is not Rejection.OK:
            self.rejected += 1
            self.rejections[verdict.name] = self.rejections.get(verdict.name, 0) + 1
            log.info("action %d (%s) rejected: %s", action.action_id, action.label, verdict.value)
            return verdict

        from gamelens.input import Sequence

        def revalidate() -> None:
            """Re-run every provenance rule at press time.

            Acceptance at submission only means the world was right *then*.
            Between then and the keystroke there is a queue and at least one
            dwell, which is ample time for the window to move, the backend to be
            replaced, the frame to go stale, or a reflex to preempt.
            """
            verdict = self.evaluate(action)
            if verdict is not Rejection.OK:
                self.rejected += 1
                self.rejections[verdict.name] = self.rejections.get(verdict.name, 0) + 1
                raise ActionRejected(verdict, f"at execution of action {action.action_id}")

        self._executor.submit(Sequence(
            steps=action.steps,
            label=f"{action.source}:{action.label}",
            meta={
                "action_id": action.action_id,
                "frame_id": action.observation.frame_id,
                "geometry_generation": action.observation.geometry_generation,
            },
            validate=revalidate,
            on_outcome=on_outcome,
        ))
        self.accepted += 1
        return Rejection.OK

    # --- building actions from model coordinates ------------------------

    def click_action(
        self,
        observation: Observation,
        x: float,
        y: float,
        *,
        label: str = "click",
        source: str = "agent",
        button=None,
    ) -> Action:
        """Turn a coordinate in the model's image into a screen-space sequence.

        Mapping happens against the geometry generation recorded in the
        observation, and ``evaluate`` refuses the action if that generation is
        no longer current -- so the mapping is never applied to a window that has
        moved since.
        """
        from gamelens.input import Button, ButtonDown, ButtonUp, Dwell, MoveTo

        import math

        button = button or Button.LEFT

        if not (math.isfinite(x) and math.isfinite(y)):
            raise ActionRejected(Rejection.OUT_OF_BOUNDS, f"non-finite coordinate ({x}, {y})")

        frame_x, frame_y = observation.to_frame_xy(x, y)

        # Bounds are a safety check, not a tidiness one. Mapping is just "add the
        # window origin", so a coordinate outside the frame produces a screen
        # point outside the game -- on top of whatever else is there. The
        # foreground interlock does not catch it, because the game really is
        # still foreground right up until the button goes down.
        if not (0 <= frame_x < observation.frame_width and
                0 <= frame_y < observation.frame_height):
            raise ActionRejected(
                Rejection.OUT_OF_BOUNDS,
                f"({frame_x:.0f}, {frame_y:.0f}) is outside the "
                f"{observation.frame_width}x{observation.frame_height} frame",
            )

        geometry = self._geometry.current
        if geometry.generation != observation.geometry_generation:
            raise ActionRejected(
                Rejection.GEOMETRY_MOVED,
                f"observed at generation {observation.geometry_generation}, "
                f"now {geometry.generation}",
            )

        screen_x, screen_y = geometry.frame_to_screen(
            frame_x, frame_y, observation.frame_width, observation.frame_height
        )

        # Belt and braces: confirm the mapped point really lands on the target.
        if not geometry.contains_screen_point(screen_x, screen_y):
            raise ActionRejected(
                Rejection.OUT_OF_BOUNDS,
                f"screen point ({screen_x}, {screen_y}) is outside the target window",
            )
        # Matches InputExecutor's defaults; see the note there on why a 16ms
        # settle produced clicks that real UI toolkits ignored outright.
        settle = getattr(self._executor, "_move_settle", 0.12)
        hold = getattr(self._executor, "_press_hold", 0.06)
        return Action(
            observation=observation,
            steps=[MoveTo(screen_x, screen_y), Dwell(settle),
                   ButtonDown(button), Dwell(hold), ButtonUp(button)],
            label=f"{label}@{int(frame_x)},{int(frame_y)}",
            source=source,
        )

    # --- keyboard and camera --------------------------------------------

    def key_action(
        self,
        observation: Observation,
        key: str,
        *,
        hold: float = 0.08,
        label: str = "key",
        source: str = "agent",
    ) -> Action:
        """Press and release one key.

        Bound to an observation like everything else. A keystroke has no
        coordinate, so none of the geometry rules apply to it -- but the
        freshness ones very much do. "Walk forward" is a decision about a scene,
        and a scene two seconds stale is one the player has already left.

        ``hold`` is clamped rather than trusted. This is the one action that can
        hold input down, and an unclamped value is how a harness ends up with W
        pressed for a minute because something upstream sent a float it did not
        mean. The executor releases held keys on a kill, but not being able to
        get into that state in the first place is better.
        """
        from gamelens.input import Dwell, KeyDown, KeyUp, key_code

        vk = key_code(key)
        hold = max(0.01, min(float(hold), MAX_KEY_HOLD))
        return Action(
            observation=observation,
            steps=[KeyDown(vk), Dwell(hold), KeyUp(vk)],
            label=f"{label}:{str(key).lower()}",
            source=source,
        )

    def press_action(
        self,
        observation: Observation,
        *,
        button: str = "left",
        hold: float = 0.08,
        label: str = "press",
        source: str = "agent",
    ) -> Action:
        """Hold a mouse button where the cursor already is. No movement at all.

        This is the action a pointer-locked game needs and ``click_action``
        cannot provide. Once a game grabs the cursor it hides it, re-centres it
        every frame and aims at the crosshair, so moving to an absolute point
        before pressing does not aim -- it *turns the player*, by whatever delta
        happens to fall out of the difference. Mining, attacking and placing all
        mean "press where I am already looking".

        The hold is what makes it useful: breaking a block is a press sustained
        for seconds, not a tap. Clamped, for the same reason a key hold is.
        """
        from gamelens.input import Button, ButtonDown, ButtonUp, Dwell

        try:
            btn = Button[str(button).strip().upper()]
        except KeyError:
            raise ActionRejected(
                Rejection.OUT_OF_BOUNDS,
                f"unknown button {button!r}; use left, right or middle",
            )
        hold = max(0.01, min(float(hold), MAX_BUTTON_HOLD))
        return Action(
            observation=observation,
            steps=[ButtonDown(btn), Dwell(hold), ButtonUp(btn)],
            label=f"{label}:{btn.name.lower()}",
            source=source,
        )

    def look_action(
        self,
        observation: Observation,
        dx: float,
        dy: float,
        *,
        label: str = "look",
        source: str = "agent",
    ) -> Action:
        """Turn the camera by a relative delta.

        Clamped for the same reason as ``hold``: a pointer-locked game applies
        the delta to its view angle directly, so a bad number does not land in
        the wrong place on screen -- it spins the player round and there is no
        coordinate check that would catch it, because there is no coordinate.
        """
        from gamelens.input import LookBy

        import math

        if not (math.isfinite(dx) and math.isfinite(dy)):
            raise ActionRejected(Rejection.OUT_OF_BOUNDS, f"non-finite delta ({dx}, {dy})")
        cdx = int(max(-MAX_LOOK_DELTA, min(MAX_LOOK_DELTA, dx)))
        cdy = int(max(-MAX_LOOK_DELTA, min(MAX_LOOK_DELTA, dy)))
        return Action(
            observation=observation,
            steps=[LookBy(cdx, cdy)],
            label=f"{label}:{cdx:+d},{cdy:+d}",
            source=source,
        )

    def stats(self) -> dict:
        with self._lock:
            preemptions = self._preemptions
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "preemptions": preemptions,
            "rejections": dict(self.rejections),
        }
