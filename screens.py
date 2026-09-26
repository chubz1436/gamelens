"""What is on the screen, judged from one frame's pixels. No I/O, no side effects.

These lived in `bot.py`, which imports `play` and reads live tokens the moment it
is imported, so nothing in here could be tested without a running server. They
take an array and return an answer; `bot.py` re-exports them under the same
names, so every driver keeps working unchanged.

Every coordinate is a fraction of the frame, measured off the demo client at
roughly 860x515 with its automatic GUI scale. A different window size or GUI
scale moves the HUD relative to the frame, and these would need re-measuring --
`in_world` would then answer False, which fails closed.
"""
import cv2
import numpy as np


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


def dead_screen(arr):
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
        return False
    h, w = arr.shape[:2]
    grey = 0
    for fy in (0.605, 0.700):            # Respawn, Title Screen
        patch = arr[int(h*fy)-4:int(h*fy)+4, int(w*0.50)-18:int(w*0.50)+18].astype(float)
        b, g, r = patch[:,:,0].mean(), patch[:,:,1].mean(), patch[:,:,2].mean()
        # 45, not 90: for its first second the buttons are drawn disabled (~60)
        if max(b,g,r) - min(b,g,r) < 30 and 45 < (b+g+r)/3 < 200:
            grey += 1
    # the death screen tints everything red; the pause menu does not
    red = arr[:int(h*0.35)].astype(float)
    tinted = red[:,:,2].mean() > red[:,:,1].mean() * 1.25
    return grey == 2 and tinted


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


# The nine hotbar slots, as fractions of the frame: x0, y0, x1, y1. Measured off
# a real 870x519 capture rather than assumed, and kept as fractions so a resized
# window does not silently move them.
HOTBAR = (0.2885, 0.898, 0.7057, 0.979)
SLOT_CHANGE = 6.0

# A count that changed: at least this many count-digit pixels differ in one
# slot, and at least DIGIT_DOMINANCE times as many as in any other slot. Live,
# dirt landing on a dirt stack changed 20-60 pixels in its slot and 0 anywhere
# else; the worst same-inventory pair on record changed 15 in one slot and 8 in
# the next, from slot borders caught in the band -- noise spread across slots.
DIGIT_CHANGE = 16
DIGIT_DOMINANCE = 3.0


class SlotReading:
    """One frame's hotbar: each slot's mean colour, and its count-digit pixels.

    Colour alone misses the commonest pickup there is. Dirt landing on a dirt
    stack changes a "2" to a "3" and nothing else; measured live, the colour
    test caught 2 of 5 real block breaks and missed every one that stacked.
    """

    __slots__ = ("colors", "digits")

    def __init__(self, colors, digits):
        self.colors = colors
        self.digits = digits


def _digit_masks(band, w):
    """Count-digit pixels per slot: white, with Minecraft's font shadow.

    Every glyph pixel of an item count is drawn pure white with a dark copy one
    GUI pixel down and to the right. Pale item icons -- wool, sand, planks --
    reach white too, but not with that shadow, and without it JPEG noise on
    those icons read as a count change in every frame.
    """
    hsv = cv2.cvtColor(np.ascontiguousarray(band), cv2.COLOR_BGR2HSV)
    v, s = hsv[:, :, 2], hsv[:, :, 1]
    k = max(1, round(w / 430))            # one GUI pixel at the demo's GUI scale
    shadow = np.zeros(v.shape, bool)
    shadow[:-k, :-k] = v[k:, k:] < 90
    mask = (v > 225) & (s < 35) & shadow
    step = mask.shape[1] / 9.0
    return [mask[2:-1, int(i * step) + 2:int((i + 1) * step) - 1] for i in range(9)]


def slots(arr):
    """Read the hotbar -- the closest thing to an inventory read.

    Not a parser: it cannot say *what* is in a slot or how many, only that a
    slot looks different than it did. That is enough to answer the question the
    drivers were answering with churn, and it answers it about the inventory
    instead of about the whole screen.
    """
    if arr is None:
        return None
    h, w = arr.shape[:2]
    fx0, fy0, fx1, fy1 = HOTBAR
    x0, x1 = int(w * fx0), int(w * fx1)
    y0, y1 = int(h * fy0), int(h * fy1)
    band = arr[y0:y1, x0:x1, :3]
    if band.size == 0:
        return None
    strip = band.astype(np.float32)
    step = strip.shape[1] / 9.0
    out = []
    for i in range(9):
        inner = strip[:, int(i * step) + 4:int((i + 1) * step) - 4]
        out.append(inner.mean(axis=(0, 1)) if inner.size else np.zeros(3, np.float32))
    return SlotReading(np.array(out), _digit_masks(band, w))


def _count_changed(before, after):
    changed = []
    for x, y in zip(before, after):
        r, c = min(x.shape[0], y.shape[0]), min(x.shape[1], y.shape[1])
        changed.append(int((x[:r, :c] ^ y[:r, :c]).sum()))
    ranked = sorted(changed, reverse=True)
    return ranked[0] >= DIGIT_CHANGE and ranked[0] >= DIGIT_DOMINANCE * max(ranked[1], 1)


def picked_up(before, after, floor=SLOT_CHANGE):
    """True when one slot changed and the others did not -- in colour, or in its count.

    The slots are semi-transparent, so the world showing through them drifts
    every time the player moves or the light changes -- and that drift moves all
    nine together. An item arriving in an empty slot moves one. Subtracting the
    median slot's change from the largest one leaves the part that is about the
    inventory, which is why this is not simply a threshold on the difference.

    An item arriving on a stack moves nothing but a digit, so the count pixels
    are compared too. Readings from before `SlotReading` (a bare colour array)
    still work, with the colour test alone.

    Returns None when either reading is missing: no evidence, not "no".
    """
    if before is None or after is None:
        return None
    b = before.colors if isinstance(before, SlotReading) else before
    a = after.colors if isinstance(after, SlotReading) else after
    deltas = np.abs(a - b).mean(axis=1)
    if float(deltas.max() - np.median(deltas)) > floor:
        return True
    if isinstance(before, SlotReading) and isinstance(after, SlotReading):
        return _count_changed(before.digits, after.digits)
    return False


def in_world(arr):
    """True when the live world is on screen, not a menu drawn over it.

    The other probes each recognise one screen and answer "not that one"; three
    of those together still said "fine" on a settings screen, and actions went
    into a menu and read as a still world. This is the positive answer.

    Every modal screen the demo has shown -- pause menu, demo dialog, death
    screen, inventory, crafting table, Options, title -- either drops the HUD or
    draws it dimmed and blurred under a dark overlay. What only an un-overlaid
    world draws is the *selected* hotbar slot's outline: a bright, unsaturated
    box one slot wide whose two sides run the height of the bar. Hearts would
    be the obvious signal and are the wrong one -- they measure health, and a
    one-heart frame is still in the world.

    A side is a column bright in at least half the bar's rows (not all: stack
    count digits sit over the right side). Two sides a slot apart, with the
    columns between them mostly dark, is the outline; a solid bright patch has
    sides everywhere and is refused by the interior test.

    Calibrated against every saved frame from two demo sessions: all in-world
    frames positive (night, underground, one heart, F3 overlay, full hotbar),
    no menu, dialog, death, inventory or settings screen positive.
    """
    if arr is None:
        return False
    h, w = arr.shape[:2]
    fx0, fy0, fx1, fy1 = HOTBAR
    x0, x1 = int(w * fx0), int(w * fx1)
    y0, y1 = int(h * fy0), int(h * fy1)
    pad = int(w * 0.02)               # the outline sits just outside the slot
    left = max(0, x0 - pad)
    band = arr[y0:y1, left:min(w, x1 + pad), :3]
    if band.size == 0:
        return False
    if _outline_in(_bright_cover(band), (x1 - x0) / 9.0):
        return True
    return _hotbar_anywhere(arr)


def _bright(band):
    hsv = cv2.cvtColor(np.ascontiguousarray(band), cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 2] > 190) & (hsv[:, :, 1] < 60)


def _bright_cover(band):
    return _bright(band).mean(axis=0)


def _outline_in(cover, step, top=None):
    """Two bright sides about one slot apart with mostly dark columns between.

    ``cover``: per column, the share of the band's rows that is bright. With
    ``top`` (per column, bright anywhere just above the band) the pair must also
    be joined by a top edge -- a closed box, not two strokes (RV03-I04).
    Vectorised over positions for each allowed gap, so the work is bounded by
    the band's width whatever the content: a bright screen used to cost a
    Python-level check per pair of its columns (RV03-I05).
    """
    n = cover.size
    side = cover >= 0.5
    if np.count_nonzero(side) < 2:
        return False
    csum = np.concatenate(([0.0], np.cumsum(cover)))
    tsum = None if top is None else np.concatenate(([0.0], np.cumsum(top)))
    for gap in range(max(6, int(np.ceil(0.85 * step))), int(np.floor(1.40 * step)) + 1):
        if gap >= n:
            break
        a = np.flatnonzero(side[:n - gap] & side[gap:])
        if a.size == 0:
            continue
        ok = (csum[a + gap - 2] - csum[a + 3]) / (gap - 5) < 0.4
        if tsum is not None:
            ok &= (tsum[a + gap + 1] - tsum[a]) / (gap + 1) >= 0.7
        if ok.any():
            return True
    return False


# Java sizes its HUD by an integer GUI scale chosen from the window size, so
# HOTBAR's fractions hold only near the 870x519 window they were measured on.
# Maximized in the test VM (1024-1280 wide frames) the fixed band read False in
# the world on every frame (2026-09-26). What holds at every size: the hotbar is
# centred and sits on the bottom of the client area, so only its scale is
# unknown, and that is what gets searched.
HOTBAR_PITCH_MIN = 8.0        # slot pitch in frame pixels (20 GUI px x scale)
HOTBAR_SEARCH_WIDTH = 1280    # larger frames are shrunk first: bounds the work


def _hotbar_anywhere(arr):
    """The `in_world` outline test, at every hotbar scale.

    Same side-and-interior test, same thresholds, applied to a centred band
    9.1 pitches wide and 1.1 high on the bottom of the frame, for pitches from
    8 px in half-pixel steps. A PrintWindow frame's 8 px window border below the
    hotbar does not matter: the band still holds most of the outline's sides,
    and searching border offsets changed no result. Checked against every Java frame on
    record -- the calibrated 856/870 captures and maximized 1024, 1040 and 1280
    wide ones through both WGC and PrintWindow: every world positive, no pause
    menu, inventory, death screen, dialog, Options or crafting screen positive.
    Unlike the fixed band it also demands the outline's top edge: a search over
    scales gets many chances, and two bright strokes on a menu's footer must not
    be enough (RV03-I04, Codex). The edge is looked for within a fifth of a
    pitch of the band's top; that held on every real frame, PrintWindow's 8 px
    border below the hotbar included.

    Tens of ms at most, and only runs when the fixed band says no.
    """
    h, w = arr.shape[:2]
    if w > HOTBAR_SEARCH_WIDTH:
        k = HOTBAR_SEARCH_WIDTH / w
        arr = cv2.resize(np.ascontiguousarray(arr[:, :, :3]),
                         (HOTBAR_SEARCH_WIDTH, max(1, int(h * k))), interpolation=cv2.INTER_AREA)
        h, w = arr.shape[:2]
    r0 = int(h * 0.75)
    bright = _bright(arr[r0:, :, :3]).astype(np.int32)
    rows = bright.shape[0]
    cum = np.vstack([np.zeros((1, w), np.int32), np.cumsum(bright, axis=0)])
    for pitch in np.arange(HOTBAR_PITCH_MIN, min(0.099 * w, rows / 1.1), 0.5):
        height = int(round(1.1 * pitch))
        pad = max(2, int(0.1 * pitch))
        xa = max(0, int(w / 2 - 4.55 * pitch) - pad)
        xb = min(w, int(w / 2 + 4.55 * pitch) + pad)
        y0 = rows - height
        t0 = max(0, int(y0 - 0.2 * pitch))
        t1 = min(rows, int(y0 + 0.2 * pitch) + 1)
        top = (cum[t1, xa:xb] - cum[t0, xa:xb]) > 0
        if _outline_in((cum[rows, xa:xb] - cum[y0, xa:xb]) / height, pitch, top):
            return True
    return False


# Bedrock scales its HUD with the window, so the hotbar is not at a fixed
# fraction of the frame the way `in_world` assumes: at 1366x720 in the test VM
# it sits lower and narrower than HOTBAR, and `in_world` answered False in the
# world on every frame. What does hold across sizes is its shape.
BEDROCK_BAND = 0.84           # search below this fraction of the height...
BEDROCK_SPAN = (0.2, 0.8)     # ...and between these fractions of the width
BEDROCK_SLOT = (0.025, 0.08)  # outline side, as a fraction of the width
BEDROCK_FILL = (0.18, 0.45)   # bright share of the outline's bounding box


def bedrock_in_world(arr):
    """True when Bedrock's live world is on screen, not a menu drawn over it.

    Same signal as `in_world` -- the selected hotbar slot's bright outline,
    which Bedrock hides for the pause menu, inventory, chat, dialogs and with
    F1, and dims while a menu fades in or out -- found by shape instead of by
    position: exactly one hollow, square, bright, unsaturated outline touching
    the bottom of the frame. The fill bounds are what separate it from the
    inventory grid, whose slot borders are hollow squares too but thin (0.08 to
    0.11 of their box against 0.25 to 0.33), and from anything filled in.

    Calibrated on Bedrock 26.51 at two window sizes (1280 and 1185 wide frames):
    world with and without an item selected, any slot, a tutorial toast, F5
    camera -- positive; pause menu, inventory with items, chat, trial dialog,
    F1, and pause or inventory still fading -- negative. The first frame of an
    inventory fading in can still read positive; one frame later it does not.
    """
    if arr is None:
        return False
    h, w = arr.shape[:2]
    y0 = int(h * BEDROCK_BAND)
    x0, x1 = int(w * BEDROCK_SPAN[0]), int(w * BEDROCK_SPAN[1])
    band = arr[y0:, x0:x1, :3]
    if band.size == 0:
        return False
    hsv = cv2.cvtColor(np.ascontiguousarray(band), cv2.COLOR_BGR2HSV)
    bright = ((hsv[:, :, 2] >= 170) & (hsv[:, :, 1] <= 60)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    found = 0
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if not (BEDROCK_SLOT[0] * w <= bw <= BEDROCK_SLOT[1] * w):
            continue
        if not 0.8 <= bw / max(bh, 1) <= 1.25:
            continue
        if y0 + y + bh < 0.95 * h:
            continue
        if BEDROCK_FILL[0] <= area / (bw * bh) <= BEDROCK_FILL[1]:
            found += 1
    return found == 1
