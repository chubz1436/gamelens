"""Respawn, get underground, and prove it before standing still.

Three spiders in one night, every one of them while mining in the open. The
only thing that has worked is a sealed hole, so this does that first and checks
the result: enclosed means the view is dark in every direction, not just the
one the camera happens to face.
"""
import pathlib, sys, time
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot


def view_mean(q=55):
    arr, _ = bot.frame(q)
    if arr is None:
        return None
    h, w = arr.shape[:2]
    return float(arr[int(h * 0.20):int(h * 0.80), int(w * 0.30):int(w * 0.75)].mean())


def enclosed():
    """Dark from every side, which open ground at night is not: the sky, the
    moon and the horizon all sit above 20 even on a black night."""
    readings = []
    for _ in range(4):
        readings.append(view_mean())
        bot.turn(90)
        time.sleep(0.35)
    if any(r is None for r in readings):
        return False, readings
    return max(readings) < 16.0, readings


arr, _ = bot.frame(50)
if arr is not None and bot.dead(arr):
    print("respawning", flush=True)
    bot.click(433, 315, label="respawn")
    time.sleep(3.5)
    a2, _ = bot.frame(60)
    if a2 is not None and bot.demo_dialog(a2):
        bot.dismiss_demo_dialog()
    time.sleep(0.6)

if bot.resume() is None:
    raise SystemExit("could not get back into the world")

bot.pitch_to(-90)
time.sleep(0.25)
for i in range(3):
    bot.mine(1.0)
    time.sleep(0.75)

ok, readings = enclosed()
print(f"  after digging: {[round(r, 1) if r else r for r in readings]}", flush=True)

if not ok:
    # Seal overhead with whatever was just dug. Slot 1 holds it: the inventory
    # was empty on respawn, so the dirt is the only thing in it.
    bot.act(kind="key", key="1", hold=0.05, label="hold dirt")
    time.sleep(0.3)
    for pitch in (45, 55, 35, 65):
        bot.pitch_to(pitch)
        time.sleep(0.3)
        bot.act(kind="press", button="right", hold=0.10, label="lid")
        time.sleep(0.5)
        ok, readings = enclosed()
        print(f"  lid at {pitch}: {[round(r, 1) if r else r for r in readings]}",
              flush=True)
        if ok:
            break

print("  SEALED" if ok else "  still exposed", flush=True)
bot.see("survive.jpg", q=85)
