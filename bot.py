"""Local reflex helpers for driving the demo.

Two things learned the hard way live in here. Hold times are the game's, not
guesses: dirt barehanded is 0.75s and wood 3.0s, and a hold shorter than that
breaks nothing while every outcome still reads "sent". And a frame fetched the
instant an action returns can still show the world before it -- the action is
acknowledged when injected, not when rendered -- so anything that reads pixels
to decide has to let the screen catch up first.
"""
import json, math, sys, time
import cv2, numpy as np
sys.path.insert(0, r"B:\AI_Agent_folder\GAME VIDEO")
import play
from screens import (CONTINUE_PLAYING, HOTBAR, RESPAWN, SLOT_CHANGE, at,
                     dead_screen, demo_dialog, grey_slab, in_world, menu_open,
                     picked_up, slots)

OP, AG = play.tokens()
DEG = 1400 / 180.0        # measured against the two pitch stops
# Render lag, as a guess: a frame read sooner shows the old view. Superseded by
# frame_after(), which waits for frames the server has actually published since
# the action; kept only for scripts that have not moved over.
SETTLE = 0.12

# How many frames past an action's after_frame to wait for. Measured 2026-09-23
# in the demo (printwindow, 59.5fps), 60 camera turns grouped by the offset
# actually served: +1 frame showed the turn 0/9 times, +2 7/11, +3..+6 40/40;
# then 20/20 on fresh trials at +3, a 62ms median wait against SETTLE's 120ms
# guess. Re-measure if the backend or the game's frame pacing changes.
AFTER_FRAMES = 3

# barehanded break times, seconds, plus margin
DIRT, WOOD, STONE_HAND = 1.30, 3.60, 8.00
VFOV = 70.0                # Minecraft's FOV setting is the vertical one


# Counted so the cost of the normal path and the cost of recovery can be
# reported separately, instead of an average that describes neither.
#
# The unit is one HTTP request, not one frame() call: frame() retries up to four
# times when the backend is mid-transition, and counting calls would report
# "one fetch" for a pass that actually made four round trips -- flattering
# exactly the number this change exists to bring down. `retries` is carried
# separately so a clean run and a stalling one do not read the same.
FETCHES = {"normal": 0, "recovery": 0, "retries": 0}
_BUCKET = "normal"                # which counter frame() charges its requests to


def counting(bucket):
    """Charge every /frame.jpg inside this block to one counter."""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        global _BUCKET
        previous, _BUCKET = _BUCKET, bucket
        try:
            yield
        finally:
            _BUCKET = previous

    return _scope()


def hold_focus():
    """Take the foreground back, and say whether it stuck.

    Singleplayer Minecraft pauses when its window is not focused, so a lost
    foreground is not just a refused action -- it is a frozen world, which reads
    as "every swing accomplished nothing" and looks exactly like a broken
    harness. SetForegroundWindow is refused outright when this process does not
    hold the foreground right; a minimize/restore cycle is an activation
    Windows does grant.
    """
    import ctypes
    import win32con, win32gui, win32process
    if win32gui.GetForegroundWindow() == play.HWND:
        return True

    # Windows refuses SetForegroundWindow to a process that does not already
    # hold the foreground. Attaching this thread's input queue to the one that
    # does makes the call legal -- the documented way to do this, and unlike a
    # minimize/restore cycle it does not tear down the window, which is what
    # kept killing the WGC capture session mid-run.
    user32 = ctypes.windll.user32
    try:
        mine = win32process.GetCurrentThreadId()
        theirs, _ = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())
        if theirs and theirs != mine:
            user32.AttachThreadInput(theirs, mine, True)
            try:
                win32gui.ShowWindow(play.HWND, win32con.SW_SHOW)
                win32gui.BringWindowToTop(play.HWND)
                win32gui.SetForegroundWindow(play.HWND)
            finally:
                user32.AttachThreadInput(theirs, mine, False)
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == play.HWND:
            return True
    except Exception:
        pass

    for _ in range(4):
        try:
            win32gui.ShowWindow(play.HWND, win32con.SW_MINIMIZE)
            time.sleep(0.2)
            win32gui.ShowWindow(play.HWND, win32con.SW_RESTORE)
            time.sleep(0.4)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() == play.HWND:
            return True
    return False


def frame(q=50, tries=4):
    """The newest frame, retried.

    /frame.jpg answers "no frame available" during a backend transition or a
    momentary capture stall, and a caller that takes that as an image gets a
    None and an AttributeError several calls later, somewhere unrelated. Retry
    here, where the cause is visible.
    """
    for attempt in range(tries):
        FETCHES[_BUCKET] += 1
        if attempt:
            FETCHES["retries"] += 1
        _, data, h = play.req(f"/frame.jpg?quality={q}", AG)
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is not None:
            return arr, h.get("x-gamelens-observation")
        time.sleep(0.25)
    return None, None


def frame_after(r, frames=None, q=50, wait_ms=500):
    """The world after an action, not merely the newest frame.

    `r` is an /act result. Its `after_frame` is the frame that was newest when
    the input was injected; this asks the server for one at least `frames`
    past it, instead of sleeping a guessed SETTLE and hoping the game drew it.

    Returns (None, None) when there is nothing to wait past -- the action was
    not sent -- or the frame did not come in time. That is missing evidence and
    is reported as missing; there is deliberately no sleep-and-grab fallback,
    which would hand back the very frame this exists to get past.
    """
    after = (r or {}).get("after_frame")
    if after is None:
        return None, None
    n = AFTER_FRAMES if frames is None else frames
    if n is None:
        raise ValueError("frame_after: pass frames= -- no measured default exists yet")
    FETCHES[_BUCKET] += 1
    status, data, h = play.req(
        f"/frame.jpg?quality={q}&after={after}&frames={n}&wait_ms={wait_ms}", AG)
    if status != 200:
        return None, None
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return None, None
    return arr, h.get("x-gamelens-observation")


def dead(arr=None):
    """`screens.dead_screen`, fetching a frame when none is given."""
    if arr is None:
        arr, _ = frame(60)
    return dead_screen(arr)


def respawn():
    hold_focus()
    with counting("recovery"):
        arr, obs = frame(35)
        if not (dead(arr) and grey_slab(arr, *RESPAWN)):
            return False
        x, y = at(arr, *RESPAWN)
        play.req("/act", AG, "POST", {"kind": "click", "x": x, "y": y,
                                      "label": "respawn", "observation_id": obs})
        time.sleep(2.5)
        arr, _ = frame(60)
    return not dead(arr)


def dismiss_demo_dialog():
    """Click Continue Playing. Never the other button.

    'Purchase Now!' sits immediately to its left and is a real storefront, so
    the coordinate here is chosen once, deliberately, and this is the only
    place in the harness that clicks anything on this dialog.

    It is located, not remembered: the click goes to the button found in the
    same frame whose observation is submitted with it, and if that button is
    not there this returns False without clicking anything. The previous
    version clicked (544, 378) unconditionally, which is the right button only
    at the one window size it was read from.
    """
    hold_focus()
    with counting("recovery"):
        arr, obs = frame(35)
        if not (demo_dialog(arr) and grey_slab(arr, *CONTINUE_PLAYING)):
            return False               # nothing verified, so nothing clicked
        x, y = at(arr, *CONTINUE_PLAYING)
        play.req("/act", AG, "POST", {"kind": "click", "x": x, "y": y,
                                      "label": "continue playing",
                                      "observation_id": obs})
        time.sleep(1.4)
        arr, _ = frame(60)
    return not demo_dialog(arr)


def resume():
    """Take the foreground, confirm we are in the world, return an observation.

    One fetch when nothing is wrong: both probes read the same array, and the
    observation that comes with it is handed to the action. Recovery re-fetches,
    and must -- `dismiss_demo_dialog()` waits 1.4s after clicking, while the
    arbiter refuses observations older than 0.8s, so a reused id would simply be
    rejected; and re-running the probes against the pre-recovery array would go
    on detecting a dialog that is already gone.
    """
    hold_focus()
    for attempt in range(3):
        with counting("recovery" if attempt else "normal"):
            arr, obs = frame(35)
        if arr is None:
            return None
        if demo_dialog(arr):
            dismiss_demo_dialog()
            continue
        if in_world(arr):
            return obs
        if dead_screen(arr):
            # The death screen's buttons read as a menu, and escape on it asks
            # "quit to title?". Dying is the caller's decision -- `respawn()`.
            return None
        if not menu_open(arr):
            # Not the world, and not a screen this knows how to leave. Send
            # nothing: every input to a screen nobody recognised is how View
            # Bobbing got switched off and the game quit to the title while
            # this reported recovery.
            return None
        # the observation from the top of this pass is seconds old at most and
        # describes the very menu being closed; fetching another would be the
        # third round trip this change exists to remove.
        play.req("/act", AG, "POST", {"kind": "key", "key": "escape", "hold": 0.08,
                                      "label": "close menu", "observation_id": obs})
        time.sleep(0.5)
    # The same two questions as every pass above, not just one of them. A
    # dismissal click that was refused leaves the demo dialog up with no pause
    # menu behind it -- menu_open alone says "fine", and the caller then sends
    # gameplay input into a dialog and is told it was sent.
    with counting("recovery"):
        arr, obs = frame(35)
    if not in_world(arr):
        return None
    return obs


def act(in_menu=False, **body):
    """One action, with the foreground re-taken first.

    Another application on this machine takes the foreground from time to time,
    and singleplayer Minecraft pauses the moment it loses it -- so a run that
    checked focus once at the start spends the rest of its steps sending
    correctly-formed actions into a paused world and reading every one as
    "nothing happened". Checking per action is cheap; the check is a single
    GetForegroundWindow unless it actually has to act.
    """
    # resume() exists to close a pause menu nobody asked for. A caller that is
    # deliberately driving a menu -- crafting, advancements -- must be able to
    # say so, or the helper closes the screen it was just asked to use.
    if in_menu:
        hold_focus()
        with counting("normal"):
            _, obs = frame(35)
    else:
        obs = resume()
        if obs is None:                      # recovery failed; nothing to aim at
            return {"verdict": "none", "outcome": "denied",
                    "detail": "could not resume the world", "churn": None}
    body["observation_id"] = obs
    r = json.loads(play.req("/act", AG, "POST", body)[1])
    if r.get("outcome") not in ("sent", "pending"):
        print(f"    !! {body.get('kind')} -> {r.get('outcome')} {r.get('detail')}", flush=True)
    return r


def do(**body):
    """Submit without checking whether we are in the world.

    For driving a menu deliberately, and for nothing else. It used to be what
    `look`, `key`, `mine` and `place` called, which meant every aiming and
    mining helper in this file could run its whole loop against a pause menu:
    the actions are accepted, the executor presses them, the world is frozen,
    and the run reports a clean "sent" for each one. That is the failure this
    file exists to prevent, built into the file.
    """
    hold_focus()
    with counting("normal"):
        _, obs = frame(35)
    body["observation_id"] = obs
    r = json.loads(play.req("/act", AG, "POST", body)[1])
    if r.get("outcome") not in ("sent", "pending"):
        print(f"    !! {body.get('kind')} -> {r.get('outcome')} {r.get('detail')}", flush=True)
    return r


def look(dx=0, dy=0):
    act(kind="look", dx=int(dx), dy=int(dy), label="bot")


def turn(deg):
    """Yaw in degrees, split so each piece stays inside the clamp."""
    n = int(abs(deg) * DEG // 500) + 1
    for _ in range(n):
        look(dx=deg * DEG / n)


def pitch_to(deg_above_horizon):
    """Absolute pitch: pin at the down stop, then climb a known number."""
    for _ in range(4):
        look(dy=600)
    remaining = (90 + deg_above_horizon) * DEG
    while remaining > 0:
        look(dy=-min(500, remaining))
        remaining -= 500


def mine(seconds, measure=False):
    return act(kind="press", button="left", hold=seconds,
               label="bot-mine", measure=measure)


def key(name, hold=0.10):
    return act(kind="key", key=name, hold=hold, label="bot-key")


def place():
    return act(kind="press", button="right", hold=0.08, label="bot-place")


def click(x, y, button="left", label="gui", in_menu=True):
    """Click a point in the frame. In a menu by default, because that is where
    a coordinate click is usually wanted -- `resume()` would close the screen
    being driven."""
    return act(in_menu=in_menu, kind="click", x=int(x), y=int(y),
               button=button, label=label)


# Minecraft's inventory is a 176x166 texture drawn at an integer scale, and
# every slot sits at a fixed offset inside it. So rather than remembering nine
# pixel coordinates -- which was finding GL037-I8 all over again, since the
# scale changes with the window and the GUI Scale setting -- find the panel in
# the frame and compute the slots from it.
#
# Offsets are the texture's own, in GUI units, of the slot's top-left corner.
GUI_W, GUI_H = 176, 166
G_HOTBAR = (8, 142)
G_INV = (8, 84)                  # first of the three 9-wide rows
G_CRAFT2 = (98, 18)              # the 2x2 grid in the player's own inventory
G_CRAFT2_OUT = (154, 28)
G_PITCH = 18


def gui_panel(arr):
    """(x0, y0, scale) of the open inventory panel, or None if none is open.

    Two earlier versions of this failed in ways worth keeping in the comment.
    Taking the first and last column of light grey stretched the panel across
    the whole screen as soon as the F3 overlay was up, because that overlay is
    light grey text everywhere. Taking the longest *unbroken* run then failed on
    the panel itself, whose background is interrupted by its own slots -- the
    slots are a darker grey, so a column crossing the grid has very little of
    the lighter one.

    So look for the shape instead: everything unsaturated and mid-bright,
    slot grey and background grey together, closed up and taken as one blob.
    The panel is the largest such thing on screen by a wide margin, and the
    world behind it is dimmed while it is open.
    """
    if arr is None:
        return None
    b = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    r = arr[:, :, 2].astype(np.int16)
    mask = (((abs(b - g) < 14) & (abs(g - r) < 14) & (b > 110) & (b < 240))
            .astype(np.uint8))
    # Open before closing: the F3 overlay is one-pixel-stroke text that the
    # closing would otherwise weld to the panel, and the merged blob's bounding
    # box is then neither the panel nor anything else. Erosion erases strokes
    # that thin and leaves a 344px slab untouched.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count < 2:
        return None
    # stats[0] is the background component.
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x0, y0, w, h, area = stats[i]
    if w < 80 or h < 80 or area < 0.6 * w * h:
        return None
    scale = w / GUI_W
    if not (1.0 < scale < 12.0):
        return None
    # The panel is 176x166. Anything a long way off that shape is something
    # else, and clicking inside it would be clicking at random.
    if abs(h / scale - GUI_H) > 24:
        return None
    return int(x0), int(y0), scale


# The crafting table's own screen, same texture size, different offsets.
G_CRAFT3 = (30, 17)              # the 3x3 grid's first slot
G_CRAFT3_OUT = (124, 35)


def gpoint(panel, gx, gy):
    """The centre of the slot whose texture corner is (gx, gy)."""
    x0, y0, scale = panel
    return int(x0 + scale * (gx + 8)), int(y0 + scale * (gy + 8))


def slot(panel, i, row="hotbar"):
    """Inventory slot i (0-8) of a row: hotbar, or inv1/inv2/inv3."""
    gx, gy = G_HOTBAR if row == "hotbar" else (
        G_INV[0], G_INV[1] + G_PITCH * {"inv1": 0, "inv2": 1, "inv3": 2}[row])
    return gpoint(panel, gx + G_PITCH * i, gy)


def craft2(panel, i):
    """One of the four 2x2 crafting slots, in reading order."""
    gx, gy = G_CRAFT2
    return gpoint(panel, gx + G_PITCH * (i % 2), gy + G_PITCH * (i // 2))


def craft3(panel, i):
    """One of the nine slots on a crafting table, in reading order."""
    gx, gy = G_CRAFT3
    return gpoint(panel, gx + G_PITCH * (i % 3), gy + G_PITCH * (i // 3))


def craft3_out(panel):
    return gpoint(panel, *G_CRAFT3_OUT)


def craft2_out(panel):
    return gpoint(panel, *G_CRAFT2_OUT)


def see(name, q=80, lift=1.0):
    arr, _ = frame(q)
    if arr is None:
        return None
    if lift != 1.0:
        arr = cv2.convertScaleAbs(arr, alpha=lift, beta=10)
    cv2.imwrite(str(play.SHOTS / name), arr)
    return arr


def grid(tiles, name, scale=0.62):
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
    cv2.imwrite(str(play.SHOTS / name),
                cv2.resize(np.vstack(rows), None, fx=scale, fy=scale))


def inventory():
    """One frame's slot reading, or None."""
    arr, _ = frame(80)
    return slots(arr)


def hotbar(name, scale=3.0):
    """The hotbar, enlarged. Item counts are two tiny digits at 856x512."""
    arr, _ = frame(90)
    if arr is None:
        return False
    h, w = arr.shape[:2]
    crop = arr[int(h * 0.88):, int(w * 0.27):int(w * 0.74)]
    crop = cv2.convertScaleAbs(crop, alpha=1.9, beta=8)
    cv2.imwrite(str(play.SHOTS / name),
                cv2.resize(crop, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_NEAREST))


def norm(arr):
    """Stretch so the scene's own highlights land near white."""
    f = arr.astype(np.float32)
    hi = max(np.percentile(f, 97), 1.0)
    return np.clip(f * (200.0 / hi), 0, 255)


def hfov(w, h):
    return math.degrees(2 * math.atan(math.tan(math.radians(VFOV / 2)) * (w / h)))


# A trunk classifier keyed on appearance used to live here and has been removed.
# It was refuted four times in one session by the world changing around it: a
# red-dominant rule written for oak against a birch forest, then a pale-column
# rule that matched sky through the canopy, then the same rule with a width
# gate that matched more sky, then a texture gate that matched falling rain --
# which is pale, vertical and streaked, and so is a birch trunk.
#
# Collision survives all four: walking forward changes the view and walking
# into a block does not, and the sky cannot fake that. Identification belongs
# in the slow tier; this tier should execute a bearing it is handed.
