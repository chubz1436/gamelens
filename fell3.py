"""Walk to the trunk I can see, and cut it.

The bearing comes from the frame rather than from a colour rule: the trunk sits
145px left of centre in an 870px frame spanning 99 degrees, which is 16.5
degrees, and its 75px width puts it about seven blocks out. Aim, close, swing --
and let the hotbar, not the churn, be the judge of whether wood was taken.
Churn says the screen changed; only the inventory says a log was picked up.

Every action goes through `bot.act`, so a lost foreground is noticed before the
swing rather than after the run: Minecraft opens its pause menu when it loses
focus and does not close it when focus comes back, and actions sent into that
menu are accepted, executed and completely inert.
"""
import time
import numpy as np
import bot


def step_forward():
    a, _ = bot.frame(40)
    bot.act(kind="key", key="w", hold=0.42, label="approach")
    time.sleep(bot.SETTLE)
    b, _ = bot.frame(40)
    if a is None or b is None:
        return None
    return float(np.abs(b.astype(np.int16) - a.astype(np.int16)).mean())


if bot.resume() is None:
    raise SystemExit("could not get into the world")

bot.pitch_to(-4)                 # trunk body sits just below the horizon
bot.turn(-16.5)
time.sleep(0.3)

for i in range(6):
    moved = step_forward()
    print(f"  approach {i}: churn {moved}", flush=True)
    if moved is not None and moved < 4.0:
        print("   -> blocked, stopping here", flush=True)
        break

took = 0
for s in range(8):
    before = bot.inventory()
    r = bot.act(kind="press", button="left", hold=4.80, label="fell", measure=True)
    if r.get("outcome") != "sent":
        print(f"  swing {s}: refused -- {r.get('detail')}", flush=True)
        continue
    time.sleep(0.9)
    got = bot.picked_up(before, bot.inventory())
    took += bool(got)
    print(f"  swing {s}: {r['outcome']} churn {r.get('churn')}  "
          f"{'TOOK ONE' if got else ('unknown' if got is None else 'nothing')}",
          flush=True)
    time.sleep(0.2)

bot.act(kind="key", key="w", hold=0.5, label="collect")
bot.act(kind="key", key="s", hold=0.4, label="collect")
time.sleep(0.4)
bot.hotbar("h4.jpg")
bot.see("g4.jpg", q=85)
print(f"  items collected: {took}", flush=True)
