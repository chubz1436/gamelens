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

from gamelens.input import KEY_NAMES

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

# A sequence is one decision about one frame, carried out over time: walk while
# turning, sprint-jump, hold attack while stepping in. Both limits exist so it
# stays one decision -- the step cap bounds the work of validating it, and the
# duration cap bounds how far past its frame it can still be acting. The
# duration is the button-hold ceiling, the longest a single action could
# already run.
MAX_SEQUENCE_STEPS = 64
MAX_SEQUENCE_SECONDS = MAX_BUTTON_HOLD

# The keys a sequence may press: every name in KEY_NAMES except the ones that
# make a chord Windows acts on itself (GL039-R1). The single-key action holds
# one key at a time, so nothing it sends can combine with anything; a sequence
# overlaps keys, and a chord the shell takes -- Alt+Tab, Alt+F4, Ctrl+Esc for
# the Start menu -- is one no later denial can undo. Alt (either side) and
# escape are the keys every such chord needs, since the Windows keys are not in
# KEY_NAMES at all; with them out, what a sequence can chord is ctrl/shift +
# something, which Windows hands to the foreground window -- the game, under
# the foreground interlock at every press. Tab is back in (GL-040): without
# alt it is only the game's key. A single `key` action still sends alt and
# escape.
#
# Not closed by this list: shift tapped five times is the Sticky Keys prompt,
# and ctrl+shift switches keyboard layout on machines configured for it. Both
# are accessibility/layout settings of the Owner's machine; the prompt takes
# the foreground, which the interlock then sees.
SEQUENCE_EXCLUDED_KEYS = frozenset({"alt", "ralt", "escape", "esc"})
SEQUENCE_KEYS = frozenset(KEY_NAMES) - SEQUENCE_EXCLUDED_KEYS

# Wheel notches per scroll: enough to cycle any hotbar or zoom all the way, few
# enough that a bad number cannot scroll a menu off the end.
MAX_SCROLL_CLICKS = 10
# Point-and-press timing inside a sequence's `click` step, the same as a click
# action's: see InputExecutor on why a shorter settle was ignored by real UI.
CLICK_SETTLE = 0.12
CLICK_HOLD = 0.06


@dataclass(frozen=True)
class PointAt:
    """A pointer move in a sequence, still in the caller's image pixels.

    ``parse_sequence`` has no observation, so it cannot know where on screen
    this is. ``sequence_action`` replaces each one with a ``MoveTo`` through the
    same bounds and geometry checks a click gets, and the executor never sees
    one.
    """

    x: float
    y: float


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
    # True for a sequence: its freshness is judged when it starts, not at every
    # press. See Arbiter.submit.
    committed_at_first_input: bool = False

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

    def evaluate(self, action: Action, *, freshness: bool = True) -> Rejection:
        """Every provenance rule, or every rule but the two about age.

        ``freshness=False`` is for the later presses of a sequence only (see
        ``submit``). It drops STALE_OBSERVATION and EXPIRED and nothing else:
        the target, the capture session, the geometry and preemption are still
        checked, because those mean the world is no longer the one reasoned
        about, however recently it was looked at.
        """
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
        if freshness and obs.age() > self._observation_deadline:
            return Rejection.STALE_OBSERVATION

        if self._geometry.generation != obs.geometry_generation:
            return Rejection.GEOMETRY_MOVED

        # Measured from when the observation was taken, not from when the
        # action was built. Building it after a 900ms inference would otherwise
        # hand a stale decision a fresh 800ms budget.
        if freshness and (time.monotonic() - obs.observed_at) > self._action_ttl:
            return Rejection.EXPIRED
        if freshness and action.age() > self._action_ttl:
            return Rejection.EXPIRED

        with self._lock:
            if self._preemptions != obs.preemption_counter:
                return Rejection.PREEMPTED

        return Rejection.OK

    def is_fresh(self, obs: Observation) -> bool:
        """Would an action built on ``obs`` right now pass the two age rules?"""
        return (obs.age() <= self._observation_deadline
                and (time.monotonic() - obs.observed_at) <= self._action_ttl)

    def rebind_rejection(self, shown: Observation, fresh: Observation) -> Rejection:
        """May an action decided on ``shown`` be carried out on ``fresh`` instead?

        Only age may differ. A model's own turn outlasts ACTION_TTL, so an agent
        acting through a tool nearly always finds the frame it looked at expired
        -- but age is the *only* thing that is allowed to have happened since.
        Everything else is compared between the two records, not just checked
        against the present: an observation that aged out *and* was preempted
        answers EXPIRED first in ``evaluate``, and re-checking only the fresh
        record would launder the preemption away (GL039-R2).
        """
        if fresh.target_hwnd != shown.target_hwnd:
            return Rejection.WRONG_TARGET
        backend = self._capture.backend
        if (fresh.backend_session_id != shown.backend_session_id
                or backend is None
                or backend.session_id != shown.backend_session_id
                or getattr(backend, "retired", False)):
            return Rejection.RETIRED_SESSION
        if (fresh.geometry_generation != shown.geometry_generation
                or (fresh.frame_width, fresh.frame_height)
                != (shown.frame_width, shown.frame_height)
                or fresh.scale != shown.scale
                or (fresh.crop_left, fresh.crop_top) != (shown.crop_left, shown.crop_top)):
            return Rejection.GEOMETRY_MOVED
        if fresh.preemption_counter != shown.preemption_counter:
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

        pressed = {"any": False}

        def revalidate() -> None:
            """Re-run every provenance rule at press time.

            Acceptance at submission only means the world was right *then*.
            Between then and the keystroke there is a queue and at least one
            dwell, which is ample time for the window to move, the backend to be
            replaced, the frame to go stale, or a reflex to preempt.

            A sequence is judged for age at its first press only. Its later
            presses are the rest of the same decision -- as the release of a
            two-second key hold already is -- and would otherwise be refused as
            EXPIRED 0.8s into any sequence that walks while it turns. Every
            other rule still runs at every press.
            """
            freshness = not (action.committed_at_first_input and pressed["any"])
            pressed["any"] = True
            verdict = self.evaluate(action, freshness=freshness)
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

        button = button or Button.LEFT
        screen_x, screen_y, frame_x, frame_y = self._map_point(observation, x, y)
        # Matches InputExecutor's defaults; see the note there on why a 16ms
        # settle produced clicks that real UI toolkits ignored outright.
        settle = getattr(self._executor, "_move_settle", CLICK_SETTLE)
        hold = getattr(self._executor, "_press_hold", CLICK_HOLD)
        return Action(
            observation=observation,
            steps=[MoveTo(screen_x, screen_y), Dwell(settle),
                   ButtonDown(button), Dwell(hold), ButtonUp(button)],
            label=f"{label}@{int(frame_x)},{int(frame_y)}",
            source=source,
        )

    def _map_point(self, observation: Observation, x: float, y: float):
        """An image coordinate as ``(screen_x, screen_y, frame_x, frame_y)``, or
        ActionRejected. Every pointer position -- a click's, or each ``move`` in
        a sequence -- comes through here, so they all get the same checks."""
        import math

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
        return screen_x, screen_y, frame_x, frame_y

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
        from gamelens.input import ButtonDown, ButtonUp, Dwell, button_from_name

        try:
            btn = button_from_name(button)
        except ValueError as exc:
            raise ActionRejected(Rejection.OUT_OF_BOUNDS, str(exc))
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

    def scroll_action(
        self,
        observation: Observation,
        clicks: float,
        *,
        horizontal: bool = False,
        label: str = "scroll",
        source: str = "agent",
    ) -> Action:
        """Turn the mouse wheel where the cursor already is.

        Clamped like a look: the wheel has no coordinate to check, and in most
        games it cycles weapons or hotbar slots, zooms, or scrolls a list.
        """
        from gamelens.input import Scroll

        n = _scroll_clicks(clicks)
        return Action(
            observation=observation,
            steps=[Scroll(n, bool(horizontal))],
            label=f"{label}:{'h' if horizontal else ''}{n:+d}",
            source=source,
        )

    def sequence_action(
        self,
        observation: Observation,
        steps: list,
        *,
        label: str = "sequence",
        source: str = "agent",
    ) -> Action:
        """Several primitives, overlapping in time, as one decision.

        ``steps`` must come from ``parse_sequence``, which is where every rule
        about what a sequence may contain lives; this binds them to the
        observation, turns each ``PointAt`` into a screen position through the
        checks a click gets, and marks the action as judged for age at its start.
        One point out of bounds refuses the whole sequence, before any of it runs.
        """
        from gamelens.input import MoveTo

        resolved = []
        for step in steps:
            if isinstance(step, PointAt):
                sx, sy, _, _ = self._map_point(observation, step.x, step.y)
                step = MoveTo(sx, sy)
            resolved.append(step)
        return Action(
            observation=observation,
            steps=resolved,
            label=f"{label}:{len(steps)} steps",
            source=source,
            committed_at_first_input=True,
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


def _number(step: dict, key: str, default=None) -> float:
    """A finite number from a step, or ValueError.

    bool is refused although it is an int: `{"ms": true}` is a caller bug,
    and reading it as 1 would hide it.
    """
    import math

    value = step.get(key, default)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{step.get('do')}: {key} must be a number")
    value = float(value)            # OverflowError for a 400-digit int is the caller's 400
    if not math.isfinite(value):
        raise ValueError(f"{step.get('do')}: {key} must be finite")
    return value


def _scroll_clicks(value) -> int:
    """Wheel notches from a caller's number: whole, finite, non-zero, clamped."""
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("scroll: clicks must be a number")
    if not math.isfinite(value) or value != int(value):
        raise ValueError("scroll: clicks must be a whole number of notches")
    n = int(value)
    if n == 0:
        raise ValueError("scroll: clicks must not be 0")
    return max(-MAX_SCROLL_CLICKS, min(MAX_SCROLL_CLICKS, n))


def sequence_points(steps) -> list[tuple[float, float]]:
    """The image coordinates a parsed sequence moves the pointer to, in order."""
    return [(s.x, s.y) for s in steps if isinstance(s, PointAt)]


def parse_sequence(spec, *, capacity: float) -> list:
    """Turn a caller's list of steps into executor steps, or raise ValueError.

    Everything is decided here, before anything is queued: a malformed step
    that raised after injection had begun would turn a delivered keystroke into
    an error and invite a retry that sends it twice.

    What comes out always releases what it pressed. Anything still down after
    the last step gets its release appended, in the order it was pressed, so a
    sequence cannot leave W held when it completes. (A kill or denial part-way
    through is the executor's release_all, as for every other action.)

    ``capacity`` is the rate limiter's bucket. A sequence with more new inputs
    than the bucket can ever hold would be refused part-way through, after some
    of it had already happened; it is refused whole instead.
    """
    from gamelens.input import (ButtonDown, ButtonUp, Dwell, KeyDown, KeyUp,
                                LookBy, Scroll, button_from_name, key_code)

    if not isinstance(spec, list) or not spec:
        raise ValueError("steps must be a non-empty list")
    if len(spec) > MAX_SEQUENCE_STEPS:
        raise ValueError(f"at most {MAX_SEQUENCE_STEPS} steps per sequence, got {len(spec)}")

    out: list = []
    # What is still down, as the release each one is owed, in the order it was
    # pressed -- one list across keys and buttons, so the automatic releases at
    # the end come out in press order whatever mix of devices was used.
    held: list = []
    names: dict[int, str] = {}

    def key(step: dict) -> int:
        name = str(step.get("key", "")).strip().lower()
        if name not in SEQUENCE_KEYS:
            raise ValueError(
                f"{step.get('do')}: key {name!r} is not allowed in a sequence "
                f"(allowed: {', '.join(sorted(SEQUENCE_KEYS))}); send alt and "
                "escape with a single `key` action"
            )
        vk = key_code(name)
        names[vk] = name
        return vk

    def button(step: dict):
        try:
            return button_from_name(step.get("button", "left"))
        except ValueError as exc:
            raise ValueError(f"{step.get('do')}: {exc}")

    def point(step: dict) -> PointAt:
        # Bounds need the observation and are checked in sequence_action; a
        # non-number is the caller's mistake and is refused here.
        return PointAt(_number(step, "x"), _number(step, "y"))

    for i, step in enumerate(spec):
        if not isinstance(step, dict):
            raise ValueError(f"step {i} must be an object")
        do = str(step.get("do", "")).strip().lower()
        if do == "key_down":
            vk = key(step)
            if KeyUp(vk) in held:
                raise ValueError(f"step {i}: key {names[vk]!r} is already down")
            held.append(KeyUp(vk))
            out.append(KeyDown(vk))
        elif do == "key_up":
            vk = key(step)
            if KeyUp(vk) not in held:
                raise ValueError(f"step {i}: key {names[vk]!r} was not pressed by this sequence")
            held.remove(KeyUp(vk))
            out.append(KeyUp(vk))
        elif do == "tap":
            vk = key(step)
            if KeyUp(vk) in held:
                raise ValueError(f"step {i}: key {names[vk]!r} is already down")
            ms = _number(step, "ms", 80)
            if ms < 0:
                raise ValueError(f"step {i}: ms must be >= 0")
            out += [KeyDown(vk), Dwell(ms / 1000.0), KeyUp(vk)]
        elif do == "button_down":
            b = button(step)
            if ButtonUp(b) in held:
                raise ValueError(f"step {i}: button {b.name.lower()} is already down")
            held.append(ButtonUp(b))
            out.append(ButtonDown(b))
        elif do == "button_up":
            b = button(step)
            if ButtonUp(b) not in held:
                raise ValueError(
                    f"step {i}: button {b.name.lower()} was not pressed by this sequence")
            held.remove(ButtonUp(b))
            out.append(ButtonUp(b))
        elif do == "move":
            out.append(point(step))
        elif do == "click":
            # A tap of a button, optionally at a point first: the step a
            # pointer-driven game is played with -- shift-click, a double click,
            # the press that ends a drag.
            b = button(step)
            if ButtonUp(b) in held:
                raise ValueError(f"step {i}: button {b.name.lower()} is already down")
            if "x" in step or "y" in step:
                out += [point(step), Dwell(CLICK_SETTLE)]
            out += [ButtonDown(b), Dwell(CLICK_HOLD), ButtonUp(b)]
        elif do == "scroll":
            horizontal = step.get("horizontal", False)
            if not isinstance(horizontal, bool):
                raise ValueError(f"step {i}: horizontal must be true or false")
            try:
                out.append(Scroll(_scroll_clicks(step.get("clicks")), horizontal))
            except ValueError as exc:
                raise ValueError(f"step {i}: {exc}")
        elif do == "look":
            dx, dy = _number(step, "dx", 0), _number(step, "dy", 0)
            out.append(LookBy(int(max(-MAX_LOOK_DELTA, min(MAX_LOOK_DELTA, dx))),
                              int(max(-MAX_LOOK_DELTA, min(MAX_LOOK_DELTA, dy)))))
        elif do == "wait":
            ms = _number(step, "ms")
            if ms < 0:
                raise ValueError(f"step {i}: ms must be >= 0")
            out.append(Dwell(ms / 1000.0))
        else:
            raise ValueError(
                f"step {i}: unknown do {do!r}; use key_down, key_up, tap, "
                "button_down, button_up, click, move, scroll, look or wait"
            )

    # Released in the order pressed.
    out += held

    total = sum(s.seconds for s in out if isinstance(s, Dwell))
    if total > MAX_SEQUENCE_SECONDS:
        raise ValueError(
            f"sequence waits {total:.2f}s in total; the limit is {MAX_SEQUENCE_SECONDS:.1f}s")
    new_inputs = sum(isinstance(s, (KeyDown, ButtonDown, LookBy, Scroll, PointAt))
                     for s in out)
    if new_inputs == 0:
        raise ValueError("sequence presses nothing")
    if new_inputs > capacity:
        raise ValueError(
            f"sequence has {new_inputs} new inputs but the rate limiter admits at most "
            f"{capacity:g} at once; split it, or start GameLens with a higher --rate"
        )
    return out
