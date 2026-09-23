"""Turn to a bearing, walk until something stops me, report what I see."""
import pathlib, sys, time
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot

bearing = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
pitch = float(sys.argv[2]) if len(sys.argv) > 2 else -6.0
steps = int(sys.argv[3]) if len(sys.argv) > 3 else 8

if bot.resume() is None:
    raise SystemExit("not in the world")

bot.pitch_to(pitch)
if bearing:
    bot.turn(bearing)
time.sleep(0.3)

for i in range(steps):
    a, _ = bot.frame(40)
    r = bot.act(kind="key", key="w", hold=0.45, label="approach")
    # The frame drawn after the step, not whatever was newest when it returned.
    b, _ = bot.frame_after(r, q=40)
    if a is None or b is None:
        print(f"  step {i}: no frame")
        continue
    moved = float(np.abs(b.astype(np.int16) - a.astype(np.int16)).mean())
    print(f"  step {i}: view moved {moved:5.1f}", flush=True)
    if moved < 3.5:
        print("   -> blocked", flush=True)
        break

bot.see("look.jpg", q=85)
