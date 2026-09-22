"""Place the table, craft a wooden pickaxe and a wooden hoe, take the table back."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot

P = 0.4


def panel():
    arr, _ = bot.frame(90)
    p = bot.gui_panel(arr)
    if p is None:
        raise SystemExit("no crafting panel on screen")
    return p


def recipe(p, planks_slot, plank_cells, sticks_slot, stick_cells, into, label):
    """Fill the 3x3 from two stacks, take the result, put it in the hotbar."""
    px, py = bot.slot(p, *planks_slot)
    bot.click(px, py, label="take planks"); time.sleep(P)
    for i in plank_cells:
        x, y = bot.craft3(p, i)
        bot.click(x, y, button="right", label="plank"); time.sleep(P)
    bot.click(px, py, label="planks back"); time.sleep(P)

    sx, sy = bot.slot(p, *sticks_slot)
    bot.click(sx, sy, label="take sticks"); time.sleep(P)
    for i in stick_cells:
        x, y = bot.craft3(p, i)
        bot.click(x, y, button="right", label="stick"); time.sleep(P)
    bot.click(sx, sy, label="sticks back"); time.sleep(P)

    ox, oy = bot.craft3_out(p)
    bot.click(ox, oy, label=f"take {label}"); time.sleep(P)
    hx, hy = bot.slot(p, into)
    bot.click(hx, hy, label=f"{label} to hotbar"); time.sleep(P)


# --- close the inventory, put the table on the ground ----------------------
arr, _ = bot.frame(90)
if bot.gui_panel(arr) is not None:
    bot.act(in_menu=True, kind="key", key="e", hold=0.06, label="close")
    time.sleep(0.9)

bot.resume()
bot.act(kind="press", button="right", hold=0.10, label="open table")
time.sleep(1.2)

p = panel()
print("table panel", p, flush=True)

# planks sit in inv3 slot 1 (birch, 4) and slot 0 (dark oak, 2); sticks hotbar 3
recipe(p, (1, "inv3"), (0, 1, 2), (3, "hotbar"), (4, 7), 0, "pickaxe")
bot.see("tool1.jpg", q=90)
recipe(p, (0, "inv3"), (0, 1), (3, "hotbar"), (4, 7), 1, "hoe")
bot.see("tool2.jpg", q=90)
print("tools done", flush=True)
