"""Does the game act on input posted to its window while it is NOT focused?

    .venv/Scripts/python.exe tools/bg_input_probe.py OUT_DIR [--url URL] [--token-file PATH]
                                                     [--game java|bedrock]

Minecraft only: its cases (E opens the inventory, a held left button cracks the
block under the crosshair) hold in both editions, and the in-the-world check is
chosen with --game -- `screens.in_world` for Java, `screens.bedrock_in_world`
for Bedrock. The Java probe must not be used on Bedrock: Bedrock scales its HUD
with the window, so at some sizes the Java probe looks at the wrong band and at
others it reads a pause menu still fading in as the world (2026-09-24/25). The
agent controls themselves are game-agnostic; this is one experiment about one game.

GL-039 part B1. The 2026-09-22 attempt concluded "no" -- every case sat at the
baseline -- but Minecraft's `pauseOnLostFocus:true` was on, so every case ran
against a paused world, where W, E, clicks and mouse motion do nothing by any
input path. This probe exists to ask the question without that confound, and
without being able to answer it wrongly in a new way:

* It never arms GameLens and never calls SendInput. Input goes only to the game
  window's own message queue with PostMessageW. Frames come from GameLens with
  the agent token.
* It refuses to run unless capture is FORCED to wgc or printwindow. MSS copies
  desktop pixels at the game's rectangle, so with the Owner's window on top it
  would score *that* window; and an unforced backend can fail over to MSS in the
  middle of a hold (GL039-R4, R6, R7).
* Every case is isolated (GL039-R5): immediately before injecting it re-checks
  the capture session, that the game is not foreground, and that the world is on
  screen -- a paused world is detected, not scored. Releases are posted in a
  finally. Afterwards it checks the session again and that the world is back.
  Any failed check makes that case and every later one `inconclusive`: never
  `supported` or `unsupported` on evidence that may be about something else.
  The foreground is watched throughout, not just at the start (GL039-I06).
* Motion is attributed, not assumed (GL039-I07): each pixel-judged case runs a
  no-input control of the same length first, must beat it on every trial, and
  a scene that moves on its own makes the case inconclusive. The after-frame is
  counted from the end of the input, not from before it (GL039-I04).

It does not change pauseOnLostFocus. That is the Owner's game setting: toggle it
in game (F3+P) before running this.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
MK_LBUTTON = 0x0001
VK_W, VK_E = 0x57, 0x45

SAFE_BACKENDS = ("wgc", "printwindow")
AFTER_FRAMES = 3                # bot.AFTER_FRAMES, measured on printwindow
# Frames to wait after the input before judging, per edition. Java's 3 is the
# render lag of a camera turn. Bedrock animates its menus: the pause menu took 4
# frames to appear and 11-13 to fade out on the VM at 32 fps (2026-09-25), and a
# judgement taken earlier sees the old screen (GL043-I01, Codex).
AFTER_FRAMES_BY_GAME = {"java": AFTER_FRAMES, "bedrock": 15}
MARGIN = 3.0                    # churn over the case's own control that counts as an effect
THUMB = (64, 36)


def capture_problem(state) -> str | None:
    """Why frames from this GameLens cannot be trusted as the game's, or None."""
    capture = state.get("capture") if isinstance(state, dict) else None
    if not isinstance(capture, dict):
        return "/state has no capture section"
    for key in ("backend", "session_id", "forced_backend"):
        if key not in capture:
            return f"/state capture has no {key!r}; this GameLens predates GL-039"
    forced = capture["forced_backend"]
    if forced is None:
        return ("capture is not forced, so it may fail over to mss mid-case; restart "
                "GameLens with --backend printwindow (or wgc)")
    if forced not in SAFE_BACKENDS:
        return f"capture is forced to {forced!r}; only {SAFE_BACKENDS} see an occluded window"
    if capture["backend"] != forced:
        return f"forced to {forced!r} but running {capture['backend']!r}"
    return None


def churn(a: np.ndarray, b: np.ndarray) -> float:
    def small(x):
        return cv2.cvtColor(cv2.resize(x[:, :, :3], THUMB, interpolation=cv2.INTER_AREA),
                            cv2.COLOR_BGR2GRAY).astype(np.int16)
    return round(float(np.abs(small(a) - small(b)).mean()), 3)


def centre_churn(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(churn of the centre fifth, churn of the whole frame)."""
    hh, ww = a.shape[:2]
    box = (slice(hh * 2 // 5, hh * 3 // 5), slice(ww * 2 // 5, ww * 3 // 5))
    return churn(a[box], b[box]), churn(a, b)


def key_lparam(vk: int, up: bool, scan_of) -> int:
    """The lParam a real keystroke carries: repeat 1, scancode, and for key-up
    the previous-state and transition bits."""
    lp = 1 | (scan_of(vk) << 16)
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


def xy(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


class _FocusSeen(Exception):
    """The game was seen in the foreground: stop sending new input."""


@dataclass
class CaseResult:
    name: str
    verdict: str                    # supported | unsupported | inconclusive
    reason: str = ""
    churn: float | None = None
    in_world_after: bool | None = None
    files: list = field(default_factory=list)
    trials: list = field(default_factory=list)


class Probe:
    """The procedure, with every outside effect behind ``io`` so it can be tested.

    ``io`` provides: state() -> dict (with capture.frame_id), frame(after=None)
    -> (array, frame_id), foreground() -> bool (is the game foreground),
    post(msg, wparam, lparam), in_world(array) -> bool, client_size() -> (w, h),
    scan(vk) -> int, sleep(seconds), save(name, array) -> str.

    Attribution (GL039-I07). Screen motion is not evidence that the input did
    anything: rain, a mob, the sun all move pixels. So every pixel-judged case
    is preceded by a *control* of the same length with no input, from the same
    starting frame; a noisy control makes the case inconclusive instead of
    letting the scene vouch for the input. The case must then beat its own
    control by MARGIN, on every one of ``trials`` repetitions, and meet its own
    specific evidence where it has one (the mining hold changes the crosshair
    region more than the frame as a whole).
    """

    def __init__(self, io, *, trials: int = 2) -> None:
        self.io = io
        self.trials = trials
        self.session = None
        self.poisoned = ""          # once set, every later case is inconclusive
        self.focused = False        # the game was seen foreground during a case
        self.mid = None             # a frame taken while the mining hold is still down

    # --- watched primitives ---------------------------------------------------

    def _watch(self) -> None:
        if self.io.foreground():
            self.focused = True

    def _post(self, msg, wparam, lparam, *, release: bool = False) -> None:
        """Post one message. Once the game has been seen in the foreground, no
        new input is sent at all -- it would be focused input, the thing this
        probe must not measure -- but a release always goes, so nothing is left
        held (GL039-I10)."""
        self._watch()
        if self.focused and not release:
            raise _FocusSeen()
        self.io.post(msg, wparam, lparam)

    def _inject(self, inject, release) -> None:
        """Run a case's input and always its release; a focus sighting ends the
        input early and is reported by the postcheck, not raised."""
        try:
            try:
                inject()
            finally:
                release()
        except _FocusSeen:
            pass

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices, checking the foreground each one (GL039-I06): an
        activation mid-hold would make the rest of the hold focused input."""
        slices = max(1, int(seconds / 0.02))
        for _ in range(slices):
            self.io.sleep(seconds / slices)
            self._watch()

    def _frame_after_now(self):
        """A frame rendered after *this moment* (GL039-I04): the marker is the
        newest frame id read now, after the input ended, and the frame served is
        AFTER_FRAMES past it -- the render lag bot.AFTER_FRAMES measured from
        the end of injection, not from before it."""
        capture = (self.io.state() or {}).get("capture") or {}
        marker = capture.get("frame_id")
        if not isinstance(marker, int):
            return None
        arr, _ = self.io.frame(after=marker)
        self._watch()
        return arr

    # --- checks -----------------------------------------------------------------

    def _session(self):
        state = self.io.state()
        problem = capture_problem(state)
        if problem:
            return None, problem
        return state["capture"]["session_id"], None

    def _precheck(self):
        """The frame to start from, or the reason this case cannot be run."""
        session, problem = self._session()
        if problem:
            return None, problem
        if session != self.session:
            return None, f"capture session changed ({self.session} -> {session})"
        if self.io.foreground():
            return None, "the game is foreground, so this would not test background input"
        before, _ = self.io.frame()
        if before is None:
            return None, "no frame"
        if not self.io.in_world(before):
            return None, "the world is not on screen (paused? a menu?)"
        return before, None

    def _postcheck(self):
        """Why the case just run cannot be trusted, or None."""
        if self.focused:
            return "the game became foreground during the case"
        session, problem = self._session()
        if problem:
            return problem
        if session != self.session:
            return f"capture session changed during the case ({session})"
        return None

    # --- one trial ----------------------------------------------------------------

    def _trial(self, name, inject, release, duration, specific, mid_at=None):
        """One control-then-input measurement. Returns (verdict, reason, evidence).

        ``mid_at`` is when the input takes its mid-hold frame; the control takes
        one at the same offset, so a specific test is judged against what the
        same region did with no input over the same span (GL040-RV02)."""
        if self.poisoned:
            return "inconclusive", f"an earlier case: {self.poisoned}", {}
        start, problem = self._precheck()
        if problem:
            self.poisoned = f"{name}: {problem}"
            return "inconclusive", problem, {}
        self.focused = False

        # Control: the same interval, nothing sent.
        control_mid = None
        if mid_at is not None:
            self._sleep(mid_at)
            control_mid = self._frame_after_now()
            self._sleep(max(0.0, duration - mid_at))
        else:
            self._sleep(duration)
        before = self._frame_after_now()
        if before is None or (mid_at is not None and control_mid is None):
            # A missing mid-interval control is not "the centre did not move":
            # without it the input's mid-hold frame has nothing to beat
            # (GL040-RV02-I01, Codex).
            what = "no control frame" if before is None else "no mid-interval control frame"
            self.poisoned = f"{name}: {what}"
            return "inconclusive", what, {}
        control = churn(start, before)
        # The most the centre moved on its own, at either sample.
        control_centre = max(centre_churn(start, f)[0]
                             for f in (control_mid, before) if f is not None)
        evidence = {"control": control, "control_centre": control_centre,
                    "files": [self.io.save(f"{name}_start", start),
                              self.io.save(f"{name}_before", before)]}

        # The preconditions again, on the frame the input will be judged from:
        # the control interval is long enough for the Owner to click the game
        # or for a menu to open, and injecting into either is the one thing
        # this probe must not do (GL039-I10).
        # (_postcheck covers focus: it was watched at every slice of the
        # control's sleep and at the frame fetch that ended it.)
        problem = self._postcheck()
        if not problem and not self.io.in_world(before):
            problem = "the world left the screen during the control interval"
        if problem:
            self.poisoned = f"{name}: {problem}"
            return "inconclusive", problem, evidence

        self.mid = None
        self._inject(inject, release)
        after = self._frame_after_now()
        problem = self._postcheck()
        if after is None or problem:
            self.poisoned = f"{name}: {problem or 'no after-frame'}"
            return "inconclusive", problem or "no after-frame", evidence
        evidence["files"].append(self.io.save(f"{name}_after", after))
        evidence["churn"] = churn(before, after)
        evidence["in_world_after"] = bool(self.io.in_world(after))

        if not evidence["in_world_after"]:
            self.poisoned = f"{name}: left the world"
            return "inconclusive", "the world is no longer on screen after the case", evidence
        if control > MARGIN:
            return ("inconclusive",
                    f"the scene moved on its own (control churn {control}); cannot attribute",
                    evidence)
        if specific is None:
            moved = evidence["churn"] > control + MARGIN
        else:
            moved, evidence["specific"] = specific(before, after, control_centre)
        if moved:
            return "supported", "", evidence
        # Not "unsupported" (GL039-I09). No lasting change is what ignored input
        # looks like -- and also what W into a wall, or a mining hold shorter
        # than the block's break time, look like with input that worked. Only a
        # positive observation is evidence here.
        return ("inconclusive",
                "no change attributable to the input; not proof it was ignored -- the "
                "stimulus may not produce one here", evidence)

    def pixel_case(self, name, inject, release, duration, specific=None,
                   mid_at=None) -> CaseResult:
        trials = [self._trial(f"{name}{i}", inject, release, duration, specific, mid_at)
                  for i in range(self.trials)]
        verdicts = {v for v, _, _ in trials}
        result = CaseResult(name, "inconclusive")
        result.trials = [{"verdict": v, "reason": r, **{k: e[k] for k in e if k != "files"}}
                         for v, r, e in trials]
        result.files = [f for _, _, e in trials for f in e.get("files", [])]
        churns = [e.get("churn") for _, _, e in trials if e.get("churn") is not None]
        result.churn = max(churns) if churns else None
        if verdicts == {"supported"}:
            result.verdict = "supported"
        else:
            reasons = [r for v, r, _ in trials if r]
            result.reason = "; ".join(reasons) or "trials disagree"
        return result

    def menu_case(self, name, inject, release, restore_inject, restore_release) -> CaseResult:
        """E: the evidence is leaving the world, which the scene cannot fake by
        moving. Restoring it is part of the case: an unverified restore makes
        the case itself inconclusive, not just the ones after it (GL039-I05)."""
        if self.poisoned:
            return CaseResult(name, "inconclusive", f"an earlier case: {self.poisoned}")
        before, problem = self._precheck()
        if problem:
            self.poisoned = f"{name}: {problem}"
            return CaseResult(name, "inconclusive", problem)
        self.focused = False
        self._inject(inject, release)
        after = self._frame_after_now()
        problem = self._postcheck()
        result = CaseResult(name, "inconclusive", files=[self.io.save(f"{name}_before", before)])
        if after is None or problem:
            self.poisoned = f"{name}: {problem or 'no after-frame'}"
            result.reason = problem or "no after-frame"
            return result
        result.files.append(self.io.save(f"{name}_after", after))
        result.churn = churn(before, after)
        result.in_world_after = bool(self.io.in_world(after))
        if result.in_world_after:
            # The one case allowed to say "unsupported": in the world, an
            # accepted E always opens the inventory, so no change here is
            # evidence rather than an unlucky stimulus (contrast GL039-I09).
            # A strong claim from one frame, so a second, later frame has to
            # agree -- a menu still on its way reads as the world (GL043-I01).
            again = self._frame_after_now()
            problem = self._postcheck()
            if problem:
                self.poisoned = f"{name}: {problem}"
                result.reason = problem
                return result
            if again is None or not self.io.in_world(again):
                self.poisoned = f"{name}: the screen was not steady after the input"
                result.reason = ("the world showed after the input and then did not; a "
                                 "menu may have been on its way")
                return result
            result.verdict = "unsupported"
            return result
        self._inject(restore_inject, restore_release)
        back = self._frame_after_now()
        problem = self._postcheck()
        if back is None or problem or not self.io.in_world(back):
            self.poisoned = f"{name}: could not restore the world"
            result.reason = (f"left the world, but {problem or 'the world was not restored'}; "
                             "the menu may have come from something else")
            return result
        result.files.append(self.io.save(f"{name}_restored", back))
        result.verdict = "supported"
        return result

    # --- the whole run ------------------------------------------------------------

    def run(self) -> dict:
        io = self.io
        session, problem = self._session()
        if problem:
            return {"refused": problem}
        self.session = session
        w, h = io.client_size()
        cx, cy = w // 2, h // 2
        scan = io.scan

        def key_down(vk):
            self._post(WM_KEYDOWN, vk, key_lparam(vk, False, scan))

        def key_up(vk):
            self._post(WM_KEYUP, vk, key_lparam(vk, True, scan), release=True)

        def nothing():
            pass

        def sweep():
            for i in range(20):
                self._post(WM_MOUSEMOVE, 0, xy(cx + (i - 10) * 15, cy))
                self._sleep(0.03)

        def left_down():
            self._post(WM_MOUSEMOVE, 0, xy(cx, cy))
            self._post(WM_LBUTTONDOWN, MK_LBUTTON, xy(cx, cy))
            self._sleep(0.6)
            # Mid-hold, while the button is still down: cracks animate on the
            # block under the crosshair for as long as it is held, whether or
            # not the hold is long enough to break it (GL039-I09).
            self.mid = self._frame_after_now()
            self._sleep(0.7)

        def at_crosshair(before, after, control_centre):
            """Mining changes the block under the crosshair: the centre fifth of
            the frame must move by the margin and by clearly more than the frame
            as a whole -- mid-hold (cracks) or after (a broken block). A camera
            turn or a scene change moves both about equally (centre ~ whole); a
            local change in the centre, a twenty-fifth of the area, leaves the
            whole far below it. And the centre must beat, by the margin, what
            the centre did by itself in the control -- an animation there
            (a mob, a flame) would pass the other two tests with the input
            ignored (GL040-RV02)."""
            detail = {}
            for label, other in (("mid", self.mid), ("after", after)):
                if other is None:
                    continue
                centre, whole = centre_churn(before, other)
                detail[label] = {"centre": centre, "whole": whole}
                if centre > control_centre + MARGIN and centre > 2 * whole:
                    return True, detail
            return False, detail

        def tap_e():
            key_down(VK_E)
            self._sleep(0.08)

        results = [
            self.pixel_case("mouse_move", sweep, nothing, 0.6),
            self.pixel_case("w_hold", lambda: (key_down(VK_W), self._sleep(1.0)),
                            lambda: key_up(VK_W), 1.0),
            self.pixel_case("left_hold", left_down,
                            lambda: self._post(WM_LBUTTONUP, 0, xy(cx, cy), release=True), 1.3,
                            specific=at_crosshair, mid_at=0.6),
            self.menu_case("e_inventory", tap_e, lambda: key_up(VK_E),
                           tap_e, lambda: key_up(VK_E)),
        ]
        return {"trials": self.trials, "margin": MARGIN,
                "cases": [r.__dict__ for r in results]}


# Which probe answers "is the world on screen" for each edition.
IN_WORLD = {
    "java": lambda screens: screens.in_world,
    "bedrock": lambda screens: screens.bedrock_in_world,
}


class LiveIO:
    """The real thing: GameLens over HTTP with the agent token, PostMessageW."""

    def __init__(self, url: str, token: str, out: Path, game: str = "java") -> None:
        import play                               # the repo's HTTP helper
        import win32api
        import win32gui
        import screens

        self._play, self._api, self._gui = play, win32api, win32gui
        self._in_world = IN_WORLD[game](screens)
        self.after_frames = AFTER_FRAMES_BY_GAME[game]
        self.url, self.token, self.out = url, token, out
        play.BASE = url
        state = self.state()
        target = state.get("target") or {}
        self.hwnd = int(target.get("hwnd") or 0)
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            raise SystemExit("GameLens reports no live target window")

    def state(self) -> dict:
        status, body, _ = self._play.req("/state", self.token)
        return json.loads(body) if status == 200 else {}

    def frame(self, after=None):
        path = "/frame.jpg?quality=90"
        if after is not None:
            path += f"&after={after}&frames={self.after_frames}&wait_ms=2000"
        status, body, headers = self._play.req(path, self.token)
        if status != 200:
            return None, None
        arr = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        frame_id = headers.get("X-GameLens-Frame") or headers.get("x-gamelens-frame")
        return arr, int(frame_id) if frame_id else None

    def foreground(self) -> bool:
        return self._gui.GetForegroundWindow() == self.hwnd

    def post(self, msg, wparam, lparam) -> None:
        self._gui.PostMessage(self.hwnd, msg, wparam, lparam)

    def in_world(self, arr) -> bool:
        return bool(self._in_world(arr))

    def client_size(self):
        left, top, right, bottom = self._gui.GetClientRect(self.hwnd)
        return right - left, bottom - top

    def scan(self, vk) -> int:
        return self._api.MapVirtualKey(vk, 0) & 0xFF

    def sleep(self, seconds) -> None:
        time.sleep(seconds)

    def save(self, name, arr) -> str:
        path = self.out / f"{name}.jpg"
        cv2.imwrite(str(path), arr)
        return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8777")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--game", choices=sorted(IN_WORLD), default="java")
    args = parser.parse_args(argv)
    import tempfile

    token_path = args.token_file or Path(tempfile.gettempdir()) / "ag.tok"
    token = token_path.read_text(encoding="utf-8").strip()
    args.out.mkdir(parents=True, exist_ok=True)
    result = Probe(LiveIO(args.url, token, args.out, args.game)).run()
    result["game"] = args.game
    result["ran_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (args.out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 1 if "refused" in result else 0


if __name__ == "__main__":
    sys.exit(main())
