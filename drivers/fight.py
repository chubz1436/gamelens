"""Swing at what is in front, and stop before it kills me.

Health is read off the HUD rather than guessed: the hearts are a row of red
pixels at a fixed place in the frame, and counting how much red is left there is
enough to know when to break off. Three deaths tonight were all the same
mistake -- standing still with no idea how much damage had landed.
"""
import pathlib, sys, time
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot

bearing = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
swings = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def hearts():
    """Fraction of the heart row that is still red."""
    arr, _ = bot.frame(55)
    if arr is None:
        return None
    h, w = arr.shape[:2]
    row = arr[int(h * 0.845):int(h * 0.865), int(w * 0.285):int(w * 0.475)]
    b, g, r = row[:, :, 0].astype(int), row[:, :, 1].astype(int), row[:, :, 2].astype(int)
    red = (r > 110) & (r - g > 55) & (r - b > 55)
    return float(red.mean())


if bot.resume() is None:
    raise SystemExit("not in the world")

bot.act(kind="key", key="1", hold=0.05, label="pickaxe")
time.sleep(0.3)
if bearing:
    bot.turn(bearing)
time.sleep(0.35)

start = hearts()
print(f"  health {start:.3f}", flush=True)

for i in range(swings):
    bot.act(kind="press", button="left", hold=0.08, label="hit", measure=True)
    time.sleep(0.62)                       # the attack cooldown, roughly
    now = hearts()
    print(f"  swing {i}: health {now:.3f}", flush=True)
    if now is not None and start and now < start * 0.55:
        print("  breaking off", flush=True)
        break

bot.see("fight.jpg", q=85)
