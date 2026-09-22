# PLAN-GL037 — three measured defects in the control loop

## Goal

Three defects, all found by benchmarking rather than by tests, all currently
green under the existing suite. Each fix must be proved by a measurement, not by
the suite going green again.

| # | Defect | Measured now | Target |
|---|---|---|---|
| 1 | `/state` fps is an artifact of its own 20Hz sampler | reports **16.0** while capture publishes **48.7** frames/s | reported rate within ±15% of the publish rate |
| 2 | `measure=True` blocks the dispatch path for a fixed 150ms | `/act look` **33.9ms** → **188.6ms** median | the 150ms stays; the caller may shorten it deliberately |
| 3 | `bot.resume()` spends two extra frame fetches per action | 3 `/frame.jpg` round trips per keystroke, `bot.act` **51.3ms** | exactly 1 fetch per action, ≤ 35ms median |

Measurements above come from `bench.py` (repo root) and a 3s comparison of
`capture.distinct` / `capture.duplicates` deltas against `/state`.

**Correction to that evidence (round 2, from finding GL037-R1).** The 3s sample
was taken on the **printwindow** backend, and only WGC publishes a real native
timespan (`capture.py:486`). `mss` and printwindow publish an ever-incrementing
counter (`capture.py:535`, `:638`), so `_publish`'s duplicate gate never fires
for them and `duplicates: 0` says nothing about distinct content. The honest
reading is **48.7 frames published per second**, not 48.7 distinct scenes.
Defect 1 is unaffected — the sampler ceiling is real and independent of content
— but its target is stated against the *publish* rate, and no claim about
distinctness is made anywhere in this plan.

## Defect 1 — fps counted by the poller instead of the publisher

**Cause.** `gamelens/app.py:218` runs `while not self._stop.wait(0.05)` and
appends at most one timestamp per iteration to `self._frame_times`. `_fps()`
divides that count by its span, so the result **cannot exceed 20** regardless of
capture. Measured: 146 frames published in 3s (48.7/s) while `/state` said
16.0. The duplicate count is not cited as evidence here; see the correction
above for why it is meaningless on this backend.

**Approach.** Timestamp where frames are actually published.
`gamelens/capture.py:406` already does `self.distinct += 1` inside `_publish`;
append `time.monotonic()` to a bounded deque beside it, and expose a rate from
`stats()`. `GameLens._fps()` reads that instead of its own sampler.

**Why not the one-line alternative.** `Δdistinct / Δt` sampled at 20Hz would also
be arithmetically correct and is a smaller change. Rejected because `distinct`
resets when a backend is swapped (`capture.py:329`), so a delta spanning a swap
goes negative — and backend swaps are exactly when someone reads this number.
A deque owned by the publisher has no cross-swap state to corrupt.

**Kept.** `_watch` still refreshes geometry and records frame ages; only the fps
responsibility moves.

## Defect 2 — the settle sleep (the one needing review)

**Cause.** `CHURN_SETTLE = 0.15` sleeps inside `_churn_since`, on the dispatch
path, for every measured action.

**Why 0.15 was chosen, and why it is wrong.** It was picked believing capture ran
at ~16fps (63ms/frame), so ~2 frames. Capture actually publishes ~48.7fps
(≈20ms/frame), so 150ms is ~7 frames — roughly 3.5x more waiting than the
purpose requires.

**Approach (revised after review — the frame-count early exit is withdrawn).**

The first version of this plan proposed exiting the settle early once two new
`frame_id`s had been published. That is unsound, and the reasoning behind it was
wrong in two separate ways:

- **A new frame id is not a new scene.** Only WGC dedups by native timespan.
  `mss` and printwindow publish an incrementing counter per capture attempt
  (`capture.py:535`, `:638`), so two new ids can be two captures of an identical
  screen 40ms apart. The "a frozen game publishes nothing" argument the early
  exit rested on holds *only* for WGC — and the benchmark that motivated it was
  run on printwindow.
- **Injection is not processing.** `InputExecutor` reports completion when the
  last event is injected (`input.py:591-658`), with no acknowledgement that the
  game read it. An early sample can therefore catch a transient that has not
  started, or one that has not finished — a false zero and a false change from
  the same mechanism.

So the 150ms stays as the default. What changes is **who owns the assumption**:
`settle_ms` becomes a per-request field on `/act`, defaulting to `CHURN_SETTLE`.
A caller that knows its target's animation timing may shorten it deliberately
and visibly; the harness stops pretending it can derive that number from frame
counts. A latency win bought with correctness is not a win here — this contract
has already been got wrong twice.

**`settle_ms` is hostile input, and is validated as such** (finding GL037-R4).
Today's `time.sleep(CHURN_SETTLE)` sits *outside* the try/except that guards the
measurement (`app.py`, immediately before `after = self._thumbnail()`). A
constant makes that harmless; a caller-supplied number does not. `time.sleep()`
raises on a negative or non-finite argument, and it would raise **after the
input had already been injected** — turning a successful action into an HTTP
500, and inviting a retry that sends the same keystroke into a game twice. That
is a worse failure than the latency it was meant to save.

So the contract is:

- `settle_ms` is optional; absent means `CHURN_SETTLE`.
- Rejected at the edge with **400** if it is not a finite number, exactly as
  `/act` already rejects a malformed `hold` or `dx` — a bad request must fail
  *before* anything is injected, not after.
- Clamped in the dispatch path to **0 … 1000ms** regardless, so no edge-case
  path can hand `time.sleep` a value it will refuse.
- The sleep moves **inside** the guard, restoring the standing invariant that
  instrumentation never fails the action.

Tests: a negative, a NaN and an absurdly large `settle_ms`; and one asserting
that a hostile value cannot convert a `sent` action into an error response.

**Invariants that must not change** (covered by `tests/test_effect.py`, 13 tests,
mutation-checked):

- churn **straddles** the action: sampled before, and after the screen settles —
  never across it, because a game animates feedback while a button is held.
- churn is `null` for refused, dry, or unmeasured actions.
- churn is evidence, never folded into `Dispatch.ok`.
- the thumbnail is a copy, not a view of a pooled buffer.
- instrumentation never fails the action.

**New tests required by the review:** distinct frame ids carrying unchanged
pixels must not be read as change; a transient that outlives two publications
must not be reported as a lasting change; and a `settle_ms` shorter than the
transient must be shown to produce the wrong answer, so the trade is documented
by a test rather than by a comment.

**Non-goal.** Changing the `/act` response shape, or deferring churn to a later
request. A token-and-resolve-later API would make the fast path free, but it
changes the contract every caller depends on and splits one action's truth
across two requests. Out of scope here.

## Defect 3 — three JPEG round trips to send one keystroke

**Cause.** `bot.act()` calls `resume()`, which calls `menu_open()` and
`demo_dialog()`; each fetches its own frame. Then `act()` fetches a third for the
observation id.

**Approach.** Make the probes pure functions of an array —
`menu_open(arr)`, `demo_dialog(arr)`, `dead(arr)` — and fetch **one** frame in
`resume()`, passed to both probes.

**One fetch describes the normal, already-resumed path only** (finding
GL037-R2). Recovery must re-fetch, for two reasons: `dismiss_demo_dialog()`
waits 1.4s after clicking (`bot.py:174-187`), while the arbiter rejects
observations older than 0.8s and source frames older than 1.0s
(`arbiter.py:221-231`), so a reused id would simply be refused; and re-running
the probes against the pre-recovery array would keep detecting a dialog that is
already gone. After any recovery action the array **and** the observation are
re-fetched, and `resume()` returns the final verified observation for `act()`.
Recovery fetches are counted and reported separately from the normal-path
target.

`bot.py` is a local driver, not part of the `gamelens` package; no server change.

## Verification

```
.venv/Scripts/python.exe -m pytest tests/ -q          # 117+ green, no regressions
.venv/Scripts/python.exe bench.py                      # per-action costs
.venv/Scripts/python.exe tools/fps_check.py            # NEW: /state fps vs publish rate
```

Acceptance, each proved by a number:

1. `tools/fps_check.py` reports `/state` fps within ±15% of the **publish**
   rate measured over ≥3s from the pool counters, with the backend named in the
   output (distinctness is not claimed).
2. `bench.py` shows `/act look, measure=True` unchanged at its default settle,
   and measurably cheaper at an explicit shorter `settle_ms` — with churn
   reported for both, against a moving and a static screen.
3. `bench.py` shows `bot.act` ≤ 35ms median **on the already-resumed path**, and
   a request counter proves exactly one `/frame.jpg` per action there; recovery
   fetches are reported as a separate figure.
4. New tests in `tests/test_effect.py` for unchanged pixels across distinct ids,
   for a transient outliving two publications, for a too-short `settle_ms`, and
   for hostile `settle_ms` values (negative, NaN, absurdly large) proving they
   cannot turn an injected action into an error response.

**`bench.py` must itself be fixed first** (finding GL037-R3). It currently
discards every return value and ignores HTTP status and outcome, while
`play.req()` returns errors without raising (`play.py:21-30`) — so a denied,
dry-run, unfocused or rate-limited action satisfies the latency threshold while
skipping churn entirely, because `_churn_since` returns null for anything not
`sent`. A benchmark that reports fast failures as fast successes is the exact
defect this whole effort exists to remove. It will validate HTTP status, require
`outcome == "sent"`, require a numeric churn on measured samples, classify
rejected/dry/pending separately, count frame requests, and stay inside the
configured action rate limit rather than disabling it.

## Assumptions and risks

- **Assumed:** ~48.7fps is the capture rate, not the game's render rate.
  Source: 146 published / 0 duplicates over 3s. If the game renders faster than
  capture delivers, the true ceiling is higher still; neither fix depends on the
  exact figure.
- **Risk:** the frame-wait could mask a genuine stall by silently taking the cap.
  Mitigated by keeping the cap equal to today's constant, so the worst case is
  exactly today's behaviour.
- **Risk:** measuring against a live game is not reproducible. The suite covers
  semantics with fakes; `bench.py` covers cost against whatever is running, and
  its numbers are reported with the target's state.
- **Not pushed.** `origin` is private; no push without the owner's word.
