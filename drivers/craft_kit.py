"""Logs -> planks -> crafting table + sticks, in the player's own 2x2 grid."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot

PAUSE = 0.35


def panel():
    arr, _ = bot.frame(90)
    p = bot.gui_panel(arr)
    if p is None:
        raise SystemExit("no inventory panel on screen")
    return p


arr, _ = bot.frame(90)
if bot.gui_panel(arr) is None:            # only open it if it is not open
    bot.resume()
    bot.act(kind="key", key="e", hold=0.06, label="open inventory")
    time.sleep(1.0)
p = panel()
print("panel", p, flush=True)

# --- every log becomes planks ---------------------------------------------
for src, dest in ((0, "inv3"), (1, "inv3")):
    x, y = bot.slot(p, src)
    bot.click(x, y, label=f"take logs {src}"); time.sleep(PAUSE)
    cx, cy = bot.craft2(p, 0)
    bot.click(cx, cy, label="into the grid"); time.sleep(PAUSE)
    ox, oy = bot.craft2_out(p)
    for _ in range(2):                       # two logs, two crafts
        bot.click(ox, oy, label="take planks"); time.sleep(PAUSE)
    sx, sy = bot.slot(p, src, dest)
    bot.click(sx, sy, label="store planks"); time.sleep(PAUSE)

bot.see("kit1.jpg", q=90)

# --- crafting table: one plank in each of the four slots -------------------
x, y = bot.slot(p, 0, "inv3")
bot.click(x, y, label="take planks"); time.sleep(PAUSE)
for i in range(4):
    cx, cy = bot.craft2(p, i)
    bot.click(cx, cy, button="right", label="one plank"); time.sleep(PAUSE)
bot.click(x, y, label="planks back"); time.sleep(PAUSE)
ox, oy = bot.craft2_out(p)
bot.click(ox, oy, label="take table"); time.sleep(PAUSE)
tx, ty = bot.slot(p, 2)
bot.click(tx, ty, label="table to hotbar"); time.sleep(PAUSE)

# --- sticks: two planks, one above the other -------------------------------
bot.click(x, y, label="take planks"); time.sleep(PAUSE)
for i in (0, 2):
    cx, cy = bot.craft2(p, i)
    bot.click(cx, cy, button="right", label="one plank"); time.sleep(PAUSE)
bot.click(x, y, label="planks back"); time.sleep(PAUSE)
bot.click(ox, oy, label="take sticks"); time.sleep(PAUSE)
sx, sy = bot.slot(p, 3)
bot.click(sx, sy, label="sticks to hotbar"); time.sleep(PAUSE)

bot.see("kit2.jpg", q=90)
print("kit done", flush=True)
