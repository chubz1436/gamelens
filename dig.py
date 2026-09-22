"""Chew into the tree, re-aiming whenever a swing hits nothing.

A swing that breaks a block leaves a hole, and the next swing through that hole
reaches nothing -- which is why every fixed-aim loop this session produced one
break and then a run of zeros. Churn makes that visible per swing, so the loop
uses it to decide where to point next.

What it must not do is count with it. Churn says pixels changed; it cannot tell
a broken block from a cloud, and the first live test of the inventory read
caught it getting this exactly wrong in both directions -- a real dirt block
came in at churn 2.50, under this file's old threshold of 3.0, and would have
been scored a miss. So aiming is churn's job and counting is the hotbar's.
"""
import time
import bot

MOVED = 3.0          # enough change to suggest the swing reached something
aim = 0
broke = 0
missed_aim = 0
bot.resume()

for step in range(26):
    before = bot.inventory()
    r = bot.act(kind="press", button="left", hold=4.8, label="dig", measure=True)
    if r.get("outcome") != "sent":
        print(f"  {step:2d} swing not sent: {r.get('detail')}", flush=True)
        continue
    time.sleep(0.9)                       # the drop has to reach the player
    took = bot.picked_up(before, bot.inventory())
    churn = r.get("churn")

    if took:
        broke += 1
        bot.act(kind="key", key="w", hold=0.22, label="advance")
        print(f"  {step:2d} TOOK ONE (churn {churn})  total {broke}", flush=True)
        continue

    if took is None:
        # No inventory reading, so nothing is known about this swing. It is not
        # a miss; it is an unanswered question, and it does not move a counter.
        print(f"  {step:2d} no inventory reading (churn {churn})", flush=True)

    # Aiming may use churn, because aiming only needs a hint: a swing that moved
    # nothing at all is pointed at air, whatever it did or did not break.
    aim += 1
    if churn is not None and churn < MOVED:
        missed_aim += 1
    bot.turn(14 if aim % 2 else -9)
    bot.act(kind="look", dx=0, dy=-40 if aim % 3 == 0 else 25, label="re-aim")
    if aim % 5 == 4:
        bot.act(kind="key", key="w", hold=0.30, label="close in")
    time.sleep(0.15)

bot.act(kind="key", key="w", hold=0.5, label="collect")
bot.act(kind="key", key="s", hold=0.5, label="collect")
time.sleep(0.5)
bot.hotbar("h7.jpg")
print(f"  blocks collected: {broke}   swings into air: {missed_aim}", flush=True)
