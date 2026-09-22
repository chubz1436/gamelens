"""Get logs, and know whether each swing actually took one.

An earlier run swung at trees for twenty minutes and came back with twenty-three
dirt and no wood, because nothing in the harness could tell a swing that broke a
block from one that hit the sky. Two things answer that now, and they answer
different questions:

  churn      the screen changed after the action settled. Useful for aiming.
  picked_up  a hotbar slot changed while the other eight did not. That is an
             item arriving, and it is the only thing here allowed to count.

Finding the tree is done by collision rather than colour. Every appearance rule
tried last session was refuted by the weather.

Every action goes through `bot.act`, which checks we are in the world first.
Taking the foreground back does not close the pause menu Minecraft opened when
it lost focus, so a loop that only calls `hold_focus()` spends its whole run
sending accepted, executed, completely inert actions into a frozen world.
"""
import time
import numpy as np
import bot

CONTACT = 6.0        # walking forward stops changing the view when blocked
WANT = 6


def walked():
    """How much walking forward changed the view: low means we are against a block."""
    a, _ = bot.frame(40)
    bot.act(kind="key", key="w", hold=0.40, label="approach")
    time.sleep(bot.SETTLE)
    b, _ = bot.frame(40)
    if a is None or b is None:
        return None
    return float(np.abs(b.astype(np.int16) - a.astype(np.int16)).mean())


if bot.resume() is None:
    raise SystemExit("could not get into the world")
bot.pitch_to(0)

logs = 0
unknown = 0
for step in range(24):
    moved = walked()
    if moved is None:
        print(f"  step {step:2d}  no frame; skipping", flush=True)
        continue
    if moved > CONTACT:
        if step % 5 == 4:
            bot.turn(22)
        continue

    before = bot.inventory()
    r = bot.act(kind="press", button="left", hold=4.80, label="wood", measure=True)
    if r.get("outcome") != "sent":
        print(f"  step {step:2d}  swing refused: {r.get('detail')}", flush=True)
        continue
    time.sleep(0.9)
    took = bot.picked_up(before, bot.inventory())
    if took is None:
        unknown += 1
    logs += bool(took)
    verdict = "TOOK ONE" if took else ("unknown" if took is None else "nothing")
    print(f"  step {step:2d}  contact (churn {moved:5.1f})  swing -> {r['outcome']} "
          f"churn {r.get('churn')}  {verdict}", flush=True)
    if logs >= WANT:
        break

bot.act(kind="key", key="w", hold=0.5, label="collect")
bot.act(kind="key", key="s", hold=0.4, label="collect")
bot.hotbar("w_inv.jpg")
print(f"  items collected: {logs}" + (f"   ({unknown} swings unscored)" if unknown else ""),
      flush=True)
