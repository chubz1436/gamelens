"""A positive answer to "am I in the world".

`resume()` used to decide it from three negatives -- not the pause menu, not the
demo dialog -- and a settings screen is neither, so actions went into a menu and
were reported as a still world. `screens.in_world` looks for the one thing only
the live world draws: the selected hotbar slot's crisp outline.

The fixtures are real frames from the demo client with everything above 80% of
the height blacked out. `in_world` reads only the hotbar band, and blanking the
rest keeps them small without moving the band.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import screens  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def load(name: str) -> np.ndarray:
    arr = cv2.imread(str(FIXTURES / f"{name}.jpg"))
    assert arr is not None, name
    return arr


@pytest.mark.parametrize("name", [
    "world_day",
    "world_night_full_hotbar",
    "world_low_health",          # one heart: hearts measure health, not presence
    "world_count_digits",        # stack digits sit over the outline's right side
    "world_item_selected",
])
def test_the_world_is_recognised(name):
    assert screens.in_world(load(name)) is True


@pytest.mark.parametrize("name", [
    "pause_menu",
    "demo_dialog",
    "death",
    "options",                   # the screen that walked the harness out of the game
    "crafting",
])
def test_a_screen_over_the_world_is_not(name):
    assert screens.in_world(load(name)) is False


def test_nothing_is_not_the_world():
    assert screens.in_world(None) is False
    assert screens.in_world(np.zeros((0, 0, 3), np.uint8)) is False


def test_a_solid_bright_band_is_not_an_outline():
    """Two bright columns a slot apart exist inside any bright patch."""
    arr = np.zeros((519, 870, 3), np.uint8)
    arr[int(519 * 0.85):, :] = 240
    assert screens.in_world(arr) is False


def test_the_outline_alone_is_enough():
    """And it is the outline, not the rest of the HUD, that the answer rests on."""
    arr = load("world_day")
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = screens.HOTBAR
    band = (slice(int(h * y0), int(h * y1)), slice(int(w * x0) - int(w * 0.02),
                                                   int(w * x1) + int(w * 0.02)))
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
    bright = (hsv[:, :, 2] > 190) & (hsv[:, :, 1] < 60)
    dimmed = arr.copy()
    dimmed[band][bright[band]] = 90         # what a menu's dark overlay does to it
    assert screens.in_world(dimmed) is False


def test_bot_still_exports_the_probes_under_their_old_names():
    """Drivers call bot.picked_up, bot.dead and friends; the move must not break them."""
    source = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf8")
    for name in ("grey_slab", "menu_open", "demo_dialog", "dead_screen", "in_world",
                 "slots", "picked_up", "HOTBAR", "SLOT_CHANGE"):
        assert name in source.split("from screens import", 1)[1].split(")", 1)[0], name


def test_the_last_slot_is_seen_too():
    """The outline sits just outside its slot, so the rightmost one needs the pad."""
    mirrored = np.ascontiguousarray(load("world_day")[:, ::-1])   # slot 1 -> slot 9
    assert screens.in_world(mirrored) is True


# --- resume() ----------------------------------------------------------------


@pytest.fixture
def bot(monkeypatch):
    """bot.py with a stand-in for `play`, which reads live tokens on import."""
    import types
    sent = []
    play = types.ModuleType("play")
    play.tokens = lambda: ("op", "ag")
    play.HWND = 1
    play.SHOTS = Path(".")
    play.req = lambda path, token, method="GET", body=None: sent.append((path, body)) or (200, b"{}", {})
    monkeypatch.setitem(sys.modules, "play", play)
    monkeypatch.delitem(sys.modules, "bot", raising=False)
    import bot as module
    monkeypatch.setattr(module, "hold_focus", lambda: True)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    module.sent = sent
    yield module
    sys.modules.pop("bot", None)


def _serve(bot, monkeypatch, names):
    frames = iter(names)
    monkeypatch.setattr(bot, "frame", lambda q=50, tries=4: (load(next(frames)), "obs"))


def test_resume_hands_back_the_observation_in_the_world(bot, monkeypatch):
    _serve(bot, monkeypatch, ["world_day"])
    assert bot.resume() == "obs"
    assert bot.sent == []


def test_resume_sends_nothing_to_a_screen_it_does_not_know(bot, monkeypatch):
    """Options passes every negative probe; the positive one refuses it."""
    _serve(bot, monkeypatch, ["options"] * 5)
    assert bot.resume() is None
    assert bot.sent == []


def test_frame_after_uses_the_measured_default(bot, monkeypatch):
    """Inspection GL038-I1: the default must be the measured one, and without a
    default a caller has to say how many frames it assumes."""
    assert bot.AFTER_FRAMES == 3
    bot.frame_after({"after_frame": 10})
    assert "after=10&frames=3&" in bot.sent[-1][0]
    monkeypatch.setattr(bot, "AFTER_FRAMES", None)
    with pytest.raises(ValueError):
        bot.frame_after({"after_frame": 10})
    assert bot.frame_after({"after_frame": None}) == (None, None)


# --- picked_up: an item that lands on a stack --------------------------------


def _took(before: str, after: str):
    return screens.picked_up(screens.slots(load(before)), screens.slots(load(after)))


@pytest.mark.parametrize("before, after", [
    ("stack_1to2_before", "stack_1to2_after"),
    ("stack_2to3_before", "stack_2to3_after"),     # the weakest: "2" and "3" share strokes
    ("new_item_before", "new_item_after"),
])
def test_a_real_pickup_is_seen(before, after):
    """Live, the colour test missed every break whose item stacked: 3 of 5."""
    assert _took(before, after) is True


@pytest.mark.parametrize("before, after", [
    ("short_hold_before", "short_hold_after"),
    ("same_inventory_a", "same_inventory_b"),      # pale icons and slot borders in the band
])
def test_nothing_taken_is_not_a_pickup(before, after):
    assert _took(before, after) is False


def test_a_bare_colour_reading_still_works():
    """Drivers holding readings from before SlotReading get the colour test alone."""
    a = screens.slots(load("stack_1to2_before"))
    b = screens.slots(load("stack_1to2_after"))
    assert screens.picked_up(a.colors, b.colors) is False    # colour alone misses it
    assert screens.picked_up(a, None) is None
