"""Walk to a trunk on a bearing and take logs from its base.

Two things learned the hard way are built in. Aim at the *base* block, because a
log broken at eye height drops inside the tree's own footprint where the other
columns block the way to it. And step into the gap afterwards: drops only come
to the player from about a block away, so the gap the log left is where the log
now is.
"""
import pathlib, sys, time
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot

bearing = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
swings = int(sys.argv[2]) if len(sys.argv) > 2 else 4

if bot.resume() is None:
    raise SystemExit("not in the world")

bot.pitch_to(-6)
if bearing:
    bot.turn(bearing)
time.sleep(0.35)

for i in range(6):
    a, _ = bot.frame(40)
    bot.act(kind="key", key="space", hold=0.05, label="hop")
    time.sleep(0.12)
    bot.act(kind="key", key="w", hold=0.42, label="approach")
    time.sleep(0.5)
    b, _ = bot.frame(40)
    if a is None or b is None:
        continue
    moved = float(np.abs(b.astype(np.int16) - a.astype(np.int16)).mean())
    print(f"  step {i}: {moved:5.1f}", flush=True)
    if moved < 3.0:
        break

got = 0
for s in range(swings):
    bot.pitch_to(-25)
    time.sleep(0.3)
    before = bot.inventory()
    r = bot.mine(bot.WOOD, measure=True)
    time.sleep(0.5)
    bot.act(kind="key", key="w", hold=0.30, label="collect")
    time.sleep(0.7)
    took = bot.picked_up(before, bot.inventory())
    got += bool(took)
    print(f"  swing {s}: churn {r.get('churn')} -> {took}", flush=True)
    bot.act(kind="key", key="s", hold=0.20, label="back")
    time.sleep(0.45)

bot.hotbar("chop_inv.png")
bot.see("chop_view.jpg", q=85)
print(f"  pickups seen: {got}", flush=True)
