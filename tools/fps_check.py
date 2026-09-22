"""Does /state's fps match the rate frames are actually published at?

GL-037 was that it did not, and could not: the figure came from `_watch`, which
wakes every 50ms, so it could never report more than 20 no matter how fast
capture ran -- it reported 16.0 for a backend publishing 48.7 frames a second.
This is the check that would have caught it, and the proof that it is fixed.

It counts published frames the only way an outside observer honestly can, by
watching the backend's own `frame_id` advance, and compares that with what
/state claims. It deliberately makes no claim about *distinct* frames: only the
WGC backend carries a native timespan that the pool can deduplicate against, so
on mss and printwindow an unchanged screen still publishes, and `duplicates: 0`
there is an artifact of the backend, not a fact about the screen.

    .venv/Scripts/python.exe tools/fps_check.py [seconds]
"""
import json, sys, time
sys.path.insert(0, r"B:\AI_Agent_folder\GAME VIDEO")
import play

OP, _ = play.tokens()


def state():
    st, body, _ = play.req("/state", OP)
    if st != 200:
        raise SystemExit(f"/state answered HTTP {st}: {body[:200]!r}")
    return json.loads(body)


def main(seconds=3.0):
    first = state()["capture"]
    t0 = time.perf_counter()
    reported = []
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.25)
        reported.append(state()["capture"].get("fps", 0.0))
    last = state()["capture"]
    elapsed = time.perf_counter() - t0

    if last.get("backend") != first.get("backend"):
        raise SystemExit(f"backend changed mid-run "
                         f"({first.get('backend')} -> {last.get('backend')}); "
                         f"counters restart on a swap, so this run proves nothing")

    published = last.get("frame_id", 0) - first.get("frame_id", 0)
    observed = published / elapsed if elapsed > 0 else 0.0
    claimed = sum(reported) / len(reported) if reported else 0.0

    print(f"  backend        {last.get('backend')}")
    print(f"  window         {elapsed:.2f}s")
    print(f"  frame_id delta {published}  -> {observed:.1f} published frames/s")
    print(f"  /state fps     {claimed:.1f} (mean of {len(reported)} samples)")
    print(f"  duplicates     {last.get('duplicates')}  (meaningful on wgc only)")

    if observed <= 0:
        raise SystemExit("nothing was published; is the target visible?")
    error = abs(claimed - observed) / observed
    print(f"  agreement      {100*(1-error):.0f}%")
    if error > 0.15:
        raise SystemExit(f"!! /state fps is off by {100*error:.0f}% -- "
                         f"GL-037 behaviour")
    print("  ok: /state reports the publisher's rate, not a sampler's ceiling")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
