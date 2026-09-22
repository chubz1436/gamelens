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

OP, AG = play.tokens()
DEG = 1400 / 180.0        # measured against the two pitch stops
SETTLE = 0.12             # render lag: a frame read sooner shows the old view

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


def grey_slab(arr, fx, fy):
    """True when a flat, unsaturated menu button sits at this fraction of the frame.

    Fractions, never pixels: every coordinate in this file was read off an
    856x512 window, and a click at a stored pixel would land somewhere else
    entirely if the game is ever resized. On the demo dialog the button 25%
    to the left of the one this aims at is "Purchase Now!", which is reason
    enough never to click a coordinate that was not verified in the frame
    being clicked.
    """
    if arr is None:
        return False
    h, w = arr.shape[:2]
    patch = arr[int(h*fy)-5:int(h*fy)+5, int(w*fx)-20:int(w*fx)+20].astype(float)
    if patch.size == 0:
        return False
    b, g, r = patch[:,:,0].mean(), patch[:,:,1].mean(), patch[:,:,2].mean()
    return max(b,g,r) - min(b,g,r) < 14 and 95 < (b+g+r)/3 < 190


def at(arr, fx, fy):
    """That fraction as pixels in this frame."""
    h, w = arr.shape[:2]
    return int(w*fx), int(h*fy)


# Where the buttons live, as fractions of the frame.
CONTINUE_PLAYING = (0.625, 0.728)      # the RIGHT-hand button on the demo dialog
RESPAWN = (0.500, 0.605)


def menu_open(arr):
    """True when the pause screen is up.

    Losing the foreground does not merely pause singleplayer Minecraft, it opens
    the Game Menu -- and taking the foreground back does not close it. Every
    action then goes to the menu, the world stays frozen, and the harness
    reports a clean "sent" for each one. That is the failure this whole session
    kept rediscovering, so it is worth detecting directly: the menu's buttons
    are large, flat, unsaturated grey slabs at fixed positions, which the world
    behind them is not.
    """
    if arr is None:
        return False
    h, w = arr.shape[:2]
    spots = [(0.50, 0.36), (0.38, 0.64), (0.62, 0.64)]   # Back to Game, Options, World Options
    grey = 0
    for fx, fy in spots:
        patch = arr[int(h*fy)-4:int(h*fy)+4, int(w*fx)-18:int(w*fx)+18].astype(float)
        if patch.size == 0:
            continue
        b, g, r = patch[:,:,0].mean(), patch[:,:,1].mean(), patch[:,:,2].mean()
        if max(b,g,r) - min(b,g,r) < 12 and 90 < (b+g+r)/3 < 190:
            grey += 1
    return grey == 3


def dead(arr=None):
    """True when the death screen is up.

    Worth its own check because the harness spent seven swings reporting a
    calm, identical "nothing happened" while the player was lying dead behind
    the screen. Minecraft does say what killed you -- the harness simply was not
    reading it. The two large buttons sit lower than the pause menu's and there
    is no third.

    Takes the array rather than fetching one: three JPEG round trips to send a
    single keystroke was most of the per-action cost, and every probe in a given
    pass should be judging the *same* screen anyway.
    """
    if arr is None:
        arr, _ = frame(60)
    if arr is None:
        return False
    h, w = arr.shape[:2]
    grey = 0
    for fy in (0.605, 0.700):            # Respawn, Title Screen
        patch = arr[int(h*fy)-4:int(h*fy)+4, int(w*0.50)-18:int(w*0.50)+18].astype(float)
        b, g, r = patch[:,:,0].mean(), patch[:,:,1].mean(), patch[:,:,2].mean()
        if max(b,g,r) - min(b,g,r) < 30 and 90 < (b+g+r)/3 < 200:
            grey += 1
    # the death screen tints everything red; the pause menu does not
    red = arr[:int(h*0.35)].astype(float)
    tinted = red[:,:,2].mean() > red[:,:,1].mean() * 1.25
    return grey == 2 and tinted


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


def demo_dialog(arr):
    """True when the demo's own splash is up.

    It reappears every time the pause menu is opened, it freezes the world
    behind it, and it is not the pause menu -- so the pause-menu detector walks
    straight past it while every action lands in a dialog. Its two buttons sit
    lower and further apart than the pause menu's single column.

    The two buttons alone are not enough, and assuming they were cost real
    damage: Options and Accessibility both carry a grey button pair at the same
    height, so this returned True on a settings screen, `resume()` "dismissed"
    it by clicking (544,378) -- which on that screen is a setting -- and the
    harness walked itself through the menus, toggled View Bobbing off and
    quit to the title screen, all while reporting recovery. The settings
    screens are a *grid*: they also have button pairs at 43% and 53% height,
    where the demo dialog has its dark text panel. Requiring those to be empty
    separates them cleanly on every screen this session has seen.
    """
    if arr is None:
        return False
    buttons = sum(grey_slab(arr, fx, 0.728) for fx in (0.372, 0.625))
    grid = any(grey_slab(arr, fx, fy)
               for fy in (0.43, 0.53) for fx in (0.372, 0.625))
    return buttons == 2 and not grid


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
        if not menu_open(arr):
            return obs
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
    if arr is None or menu_open(arr) or demo_dialog(arr):
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


# The nine hotbar slots, as fractions of the frame: x0, y0, x1, y1. Measured off
# a real 870x519 capture rather than assumed, and kept as fractions so a resized
# window does not silently move them.
HOTBAR = (0.2885, 0.898, 0.7057, 0.979)
SLOT_CHANGE = 6.0


def slots(arr):
    """Mean colour of each hotbar slot -- the closest thing to an inventory read.

    Not a parser: it cannot say *what* is in a slot, only that a slot looks
    different than it did. That is enough to answer the question the drivers
    were answering with churn, and it answers it about the inventory instead of
    about the whole screen.
    """
    if arr is None:
        return None
    h, w = arr.shape[:2]
    fx0, fy0, fx1, fy1 = HOTBAR
    x0, x1 = int(w * fx0), int(w * fx1)
    y0, y1 = int(h * fy0), int(h * fy1)
    strip = arr[y0:y1, x0:x1].astype(np.float32)
    if strip.size == 0:
        return None
    step = strip.shape[1] / 9.0
    out = []
    for i in range(9):
        inner = strip[:, int(i * step) + 4:int((i + 1) * step) - 4]
        out.append(inner.mean(axis=(0, 1)) if inner.size else np.zeros(3, np.float32))
    return np.array(out)


def picked_up(before, after, floor=SLOT_CHANGE):
    """True when one slot changed and the others did not.

    The slots are semi-transparent, so the world showing through them drifts
    every time the player moves or the light changes -- and that drift moves all
    nine together. An item arriving moves one. Subtracting the median slot's
    change from the largest one leaves the part that is about the inventory,
    which is why this is not simply a threshold on the difference.

    Returns None when either reading is missing: no evidence, not "no".
    """
    if before is None or after is None:
        return None
    deltas = np.abs(after - before).mean(axis=1)
    return float(deltas.max() - np.median(deltas)) > floor


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
