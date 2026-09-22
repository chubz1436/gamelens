"""Where does the time in one character action actually go?

The harness answers an action in about 23ms, which was measured earlier and is
several times faster than a human plays. That number is also misleading on its
own, because it times the action and not the loop around it -- and the loop is
what actually drives the character. This takes the loop apart.

The first version of this file timed calls and nothing else: it never looked at
an HTTP status or an outcome, so a run in which every action was refused for
being out of the foreground produced the same tidy table as a run in which every
action landed -- and the refusals were *faster*, which made a broken harness look
like an improvement. Every sample here is now checked before it is allowed to
count, and anything that did not land is reported as its own category rather
than averaged into the number.
"""
import json, statistics, time
import cv2, numpy as np
import play

OP, AG = play.tokens()

FRAME_REQUESTS = 0          # every /frame.jpg this process asks for


def req(path, token, method="GET", body=None):
    """play.req, counting frame fetches.

    The point of the bot.py change was to stop paying for three JPEG round trips
    per keystroke, so the count is the measurement -- a timing that got faster
    because a fetch silently failed would otherwise read as a win.
    """
    global FRAME_REQUESTS
    if path.startswith("/frame.jpg"):
        FRAME_REQUESTS += 1
    return play.req(path, token, method, body)


def rate_limit():
    """Actions per second the supervisor will admit.

    Read rather than assumed: the bucket is built with capacity == rate, so a
    full bucket on an idle harness *is* the rate. If anything about that changes
    the fallback is the documented default, and the bench paces itself either
    way -- a bench that outruns the limiter measures the limiter.
    """
    st, body, _ = req("/state", OP)
    if st != 200:
        return 10.0
    return float(json.loads(body).get("safety", {}).get("tokens") or 10.0)


RATE = rate_limit()
SPACING = 1.0 / max(RATE, 1.0)


def timed(fn, n=12, pace=False):
    """Run fn n times, returning one (ms, result) pair per sample.

    The duration stays attached to its own result all the way to the summary.
    Averaging first and classifying afterwards was the defect in the previous
    version of this file: a run in which every action was refused produced a
    fast, tidy median -- faster than a working run, because a refusal returns
    sooner than a keystroke -- with the warning printed beside the number rather
    than instead of it.

    Pacing sleeps *between* samples, never inside one, so it lengthens the run
    without touching any measurement.
    """
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t
        samples.append((dt * 1000, result))
        if pace:
            time.sleep(max(0.0, SPACING - dt))
    return samples


def stats(samples, valid=lambda r: True):
    """(min, median, max) over the samples that count, or None if too few."""
    xs = [ms for ms, r in samples if valid(r)]
    if len(xs) < 3:
        return None
    return min(xs), statistics.median(xs), max(xs)


def show(name, xs, note=""):
    if xs is None:
        print(f"  {name:<44} {'too few valid samples':>23}  {note}")
        return
    lo, mid, hi = xs
    print(f"  {name:<44} {lo:7.1f} {mid:7.1f} {hi:7.1f} ms  {note}")


def fetch(q=35):
    st, data, h = req(f"/frame.jpg?quality={q}", AG)
    if st != 200:
        return {"ok": False, "why": f"HTTP {st}"}
    return {"ok": True, "obs": h.get("x-gamelens-observation"), "data": data}


def act(measure=False, settle_ms=None):
    """One /act, reported as what actually happened to it."""
    f = fetch()
    if not f["ok"]:
        return {"class": "no frame", "why": f["why"]}
    body = {"kind": "look", "dx": 1, "dy": 0, "label": "bench",
            "measure": measure, "observation_id": f["obs"]}
    if settle_ms is not None:
        body["settle_ms"] = settle_ms
    st, raw, _ = req("/act", AG, "POST", body)
    if st != 200:
        return {"class": f"HTTP {st}", "why": raw[:120].decode("utf-8", "replace")}
    r = json.loads(raw)
    out, churn = r.get("outcome"), r.get("churn")
    if out == "dry":
        # Dispatch reports a dry run in `outcome`; the earlier check read
        # `verdict`, which never carries it, so a dry-run bench would have
        # counted every sample as a real press.
        return {"class": "dry run", "churn": churn}
    if out == "sent":
        # A measured sample without a number is a failed measurement, not a
        # cheap one; it must not quietly join the timings it would flatter.
        if measure and not isinstance(churn, (int, float)):
            return {"class": "sent, churn missing", "churn": None}
        return {"class": "sent", "churn": churn}
    if out == "pending":
        return {"class": "pending", "churn": churn}
    return {"class": f"refused: {r.get('detail') or out}", "churn": churn}


def landed(r):
    """The only samples a latency figure may be computed from."""
    return isinstance(r, dict) and r.get("class") == "sent"


def verdict(samples):
    """A one-line census of what the samples actually were."""
    results = [r for _, r in samples]
    kinds = {}
    for r in results:
        kinds[r["class"]] = kinds.get(r["class"], 0) + 1
    churns = [r["churn"] for r in results
              if r.get("churn") is not None and r["class"] == "sent"]
    note = " ".join(f"{k}x{v}" for k, v in sorted(kinds.items()))
    if churns:
        note += f"  churn {min(churns):.2f}-{max(churns):.2f}"
    if set(kinds) != {"sent"}:
        note = "!! " + note                # anything but a clean run is flagged
    return note


def screen_noise(gap=0.15, n=6):
    """How much the screen changes on its own over one settle interval.

    Churn is only evidence that an *action* moved the world if the world was not
    already moving: rain, mobs, a swaying hand and the sun all produce churn on
    a completely idle harness. This is the floor to read the numbers above
    against, measured the same way -- greyscale 64x36, mean absolute difference.
    """
    def thumb():
        f = fetch(35)
        if not f["ok"]:
            return None
        a = cv2.imdecode(np.frombuffer(f["data"], np.uint8), cv2.IMREAD_COLOR)
        if a is None:
            return None
        small = cv2.resize(a, (64, 36), interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)

    diffs = []
    for _ in range(n):
        a = thumb()
        time.sleep(gap)
        b = thumb()
        if a is not None and b is not None:
            diffs.append(float(np.abs(b - a).mean()))
    return statistics.median(diffs) if diffs else None


print(f"  rate limit {RATE:.0f}/s -> pacing actions {SPACING*1000:.0f}ms apart\n")
print(f"  {'step':<44} {'min':>7} {'med':>7} {'max':>7}")

def served(r):
    return r["ok"]


samples = timed(fetch)
show("GET /frame.jpg q=35 (one observation)", stats(samples, served),
     "" if all(served(r) for _, r in samples) else "!! some fetches failed")
samples = timed(lambda: fetch(60))
show("GET /frame.jpg q=60", stats(samples, served),
     "" if all(served(r) for _, r in samples) else "!! some fetches failed")
show("GET /state", stats(timed(lambda: req("/state", OP))))

for label, fn in (("no measure", lambda: act(False)),
                  ("measure=True", lambda: act(True)),
                  ("measure + settle_ms=40", lambda: act(True, settle_ms=40))):
    samples = timed(fn, 10, pace=True)
    show(f"POST /act  look, {label}", stats(samples, landed), verdict(samples))

import bot


def with_fetch_counts(fn, n):
    """Run fn n times, recording what each call cost in HTTP frame requests.

    Per call, not per batch: one recovery pass costs eight requests, and a batch
    total divided by n would report that as everybody paying a little. The
    one-request claim is only about calls that needed no recovery, so the
    samples have to be separable.
    """
    samples = []
    for _ in range(n):
        before = dict(bot.FETCHES)
        t = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t
        after = dict(bot.FETCHES)
        samples.append((dt * 1000, {
            "result": result,
            "normal": after["normal"] - before["normal"],
            "recovery": after["recovery"] - before["recovery"],
            "retries": after["retries"] - before["retries"],
        }))
        time.sleep(max(0.0, SPACING - dt))
    return samples


def normal_path(r):
    return r["recovery"] == 0


def sent(r):
    return isinstance(r["result"], dict) and r["result"].get("outcome") == "sent"


def fetch_note(samples, want_sent=False):
    clean = [r for _, r in samples if normal_path(r)]
    recovered = len(samples) - len(clean)
    counts = sorted({r["normal"] for r in clean}) or ["-"]
    note = f"{len(clean)}/{len(samples)} clean, {counts} request(s) each"
    if recovered:
        note = "!! " + note + f", {recovered} needed recovery"
    retries = sum(r["retries"] for _, r in samples)
    if retries:
        note += f", {retries} retries"
    if want_sent:
        note = f"{sum(1 for _, r in samples if sent(r))}/{len(samples)} sent, " + note
    return note


samples = with_fetch_counts(bot.resume, 6)
show("bot.resume()  (menu + demo-dialog probes)",
     stats(samples, normal_path), fetch_note(samples))

samples = with_fetch_counts(lambda: bot.act(kind="look", dx=1, dy=0, label="bench"), 6)
show("bot.act(look)  full helper",
     stats(samples, lambda r: normal_path(r) and sent(r)),
     fetch_note(samples, want_sent=True))

noise = screen_noise()
st, body, _ = req("/state", OP)
c = json.loads(body)["capture"] if st == 200 else {}
print(f"\n  capture: {c.get('backend')} publishing {c.get('fps', 0):.1f} fps "
      f"-> {1000/max(c.get('fps', 1), 1):.0f} ms between published frames")
print("  (published, not distinct: only the WGC backend deduplicates, so on "
      "mss and printwindow\n   an unchanged screen still publishes)")
print(f"  idle screen churn over 150ms: "
      + ("n/a" if noise is None else f"{noise:.2f}")
      + "  <- the floor an action's churn has to beat")
print(f"  {FRAME_REQUESTS} frame requests from this bench process")
