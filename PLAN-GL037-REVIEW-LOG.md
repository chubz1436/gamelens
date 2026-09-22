# PLAN-GL037 — review log (append-only)

## Round 0 — setup

- **Host / planner:** Claude Code (Opus 5), this session.
- **Plan reviewer:** Codex CLI 0.154.0, requested model `gpt-6-astra`.
- **Builder:** claude (host).
- **Final inspector:** fresh Codex session.
- **Plan:** `PLAN-GL037.md` · **Log:** this file · **Max rounds:** 3 · **Inspection:** on.
- **Authorization:** owner asked for the three defects to be fixed; implementation
  is authorized. Push is **not** — `origin` stays untouched without a separate ask.
- **Scope:** GL-037 fps sampler, the 150ms churn settle, and bot.py's three
  frame fetches per action. All three were found by measurement, not by tests.
- **Verification contract:** pytest 117+ green, plus a measurement per fix
  (`bench.py`, and a new `tools/fps_check.py`).

## Round 1 — REVISE (session 01a0c982-7301-7853-96c0-c3e5478c8e85)

Plan SHA256 `91657d2f…`. Codex CLI 0.154.0, requested `gpt-6-astra` effort high.
Artifacts: `%TEMP%\claudex-pbsrtplx`. Elapsed 112.9s. `observed_models: []` — the
provider returned no model identity, so the model actually used is **unverified**;
only the request is known.

Three findings, **all accepted**:

- **GL037-R1 (high)** — the frame-count early exit was unsound. Only WGC dedups
  by native timespan (`capture.py:486`); mss and printwindow publish an
  ever-incrementing counter (`:535`, `:638`), so two new frame ids can be two
  captures of an identical screen. Verified independently before accepting. This
  also corrected my own evidence: the 3s sample was taken on printwindow, where
  `duplicates: 0` proves nothing about distinct content. Early exit **withdrawn**;
  150ms default retained; `settle_ms` exposed per request instead.
- **GL037-R2 (medium)** — reusing one observation across `resume()` breaks
  recovery: `dismiss_demo_dialog()` sleeps 1.4s while the arbiter rejects
  observations older than 0.8s. One-fetch target scoped to the already-resumed
  path; recovery re-fetches and reports separately.
- **GL037-R3 (medium)** — `bench.py` discards results and ignores HTTP status and
  outcome, so denied/dry actions satisfy the latency threshold while skipping
  churn. The benchmark had the same defect the work exists to remove. Fixing it
  became a listed deliverable, ordered before the criteria that depend on it.

## Round 2 — REVISE (same session, resumed)

Plan SHA256 `5e30ab7b…`. Artifacts: `%TEMP%\claudex-owzugecm`. R1–R3 resolved.

- **GL037-R4 (medium)** — accepted. `settle_ms` had no type, finiteness or
  bounds, and `time.sleep(CHURN_SETTLE)` sits *outside* the try/except guarding
  the measurement. A negative or NaN value would raise **after injection**,
  converting a successful action into an HTTP 500 and inviting a retry that
  sends the same keystroke into a game twice. Contract specified: 400 at the
  edge for a non-finite value (fails before injection), clamp 0…1000ms in
  dispatch, and the sleep moves inside the guard.

## Round 3 — APPROVED (same session, resumed)

Plan SHA256 `07cbd5bf9c25611d9630d6551df12389502805ddaf492b36422954e3cc374e30`.
Artifacts: `%TEMP%\claudex-xhshgg00`. Verdict APPROVED, no findings.

> "Pre-injection finite-number validation, bounded settling, and guarded sleep
> address R4. No material unresolved plan defects were identified."

Approval is bound to that exact hash. Rounds used: 3 of 3.

## Phase 3 — build

Builder: claude (host). Inspector: fresh Codex session, after the build.

Pre-build commit `864cbcd`. Proof commands: `pytest tests/ -q`, `bench.py`,
`tools/fps_check.py`.

### Inspection round 1 — REVISE (Codex, gpt-6-astra, high, fresh session)

Artifacts: `…scratchpad/inspect1/claudex-yy2ec6bw`. Seven findings, all accepted;
two of them describe defects that predate this build.

| id | severity | disposition |
|---|---|---|
| GL037-I1 | high | **Accepted.** `gamelens/winput.py` and `tools/winput_probe.py` inject key and mouse events directly, bypassing arming, dry-run, the kill latch and the executor's held-input tracking, with no `finally` to release a held key. They are a dead experiment — measured ineffective against SDL earlier today — and they were never part of GL-037. Moved out of the repository rather than left lying next to a safety supervisor they ignore. |
| GL037-I2 | high | **Accepted, and it predates this build.** `submit_observation_click` forwarded `measure` (and, after my change, `settle`) to `_dispatch` without declaring either, so every successfully built click through the in-process path raised `NameError` before dispatch — a logged failure in the reflex loop, a dead worker in the vision tier. Shipped in `864cbcd` with GL-036 and crossed by no test, because the HTTP surface uses `submit_click`. Both parameters are now declared, with a test asserting the signature. |
| GL037-I3 | medium | **Accepted.** `float()` raises `OverflowError`, not `ValueError`, on an integer too large to represent — and `{"settle_ms": 1e400}` written as a JSON integer literal is valid JSON. That was a 500 at the edge and, worse, a raise *after* injection in the dispatch path. `OverflowError` added to both handlers; the hostile-value test now includes `10**400`. |
| GL037-I4 | medium | **Accepted.** `bench.py` computed its statistics before classifying, so refused, dry and pending samples still moved the median — and refusals are *faster* than presses, which is how a broken harness comes out looking quick. Durations now stay attached to their own result, statistics run over landed samples only, and a row with fewer than three valid samples prints "too few valid samples" instead of a number. It also checked `verdict == "dry"`, a field that never carries it; `outcome` is the right one. |
| GL037-I5 | medium | **Accepted.** `bot.FETCHES` counted `frame()` calls, but `frame()` retries up to four times — so a stalling pass reported "one fetch". Counting moved into `frame()` itself, one increment per HTTP request, with retries counted separately, and `bench.py` records the count per call so the one-request claim is only made about calls that needed no recovery. |
| GL037-I6 | medium | **Accepted.** After three recovery attempts `resume()` checked only `menu_open` on its final frame. A refused dismissal leaves the demo dialog up with no pause menu behind it, so the check said "fine" and the caller sent gameplay input into a dialog. The final frame now gets both checks. |
| GL037-I7 | medium | **Accepted.** The two new effect tests asserted on pixels without a publication timeline: one could not distinguish frames at all, the other changed pixels permanently and so proved only that a permanent change is reported. Replaced with a `_Publisher` stand-in that publishes on a fixed interval, carries frame ids, and measures time from injection. The pair now shows the *same* transient — one outlasting three publications — answered with 0.00 at the default settle and above the floor at `settle_ms=0`, which is the trade the knob makes. |

Coverage claimed: all 15 manifest files, SHA256 verified, plus `play.py`,
`agent.py`, `arbiter.py`, `input.py`, `safety.py`, `__main__.py`. Limitation
stated and accurate: static review only, no tests or benchmarks run — the host
ran those.

Host note on a finding *not* raised: the false-positive `demo_dialog()` probe
that walked the game through its own settings menus was found and fixed during
the host's proof runs, before this inspection; see the vault note for the
sequence.

### Inspection round 2 — REVISE (Codex, gpt-6-astra, high, fresh session)

Artifacts: `…scratchpad/inspect2`. All seven round-1 findings are gone from the
report; 13 manifest hashes verified. Three new findings, all in the local
drivers rather than the `gamelens` package.

| id | severity | disposition |
|---|---|---|
| GL037-I8 | medium | **Accepted and fixed** (after the inspection — see below). `dismiss_demo_dialog()` located the dialog in one frame and then clicked a coordinate read off an 856x512 window, and `respawn()` did the same. Both now locate the button in the frame whose observation they submit, click the fraction rather than the pixel, and refuse to click at all when the button is not verified there. This one was worth the unreviewed edit: the button 25% to the left of the one being aimed at is "Purchase Now!". |
| GL037-I9 | medium | **Accepted, not fixed. Open.** `wood.py` and `fell3.py` call `hold_focus()` and then drive gameplay through private helpers that skip `bot.resume()` — so starting either after a focus loss sends accepted actions into the pause menu, which is the exact failure the rest of this work exists to detect. They are single-session scripts from today's demo run, not part of GL-037; routing them through `bot.act()` is a small change and should happen before either is run again. |
| GL037-I10 | medium | **Accepted, not fixed. Open, and the more interesting one.** `dig.py` and `wood.py` count churn above a threshold as a broken block, and `wood.py` stops after six of them. Churn is evidence of pixel change and nothing more — a swing at dirt, a mob shoving the camera or a cloud passing can all clear the threshold, and a distant block breaking might not. Using it as a success counter is the same category error GL-036 was written to expose, committed by the drivers that consume GL-036. They need a real confirmation (inventory or block state) before counting anything. |

**Inspection budget exhausted: 2 of 2.** I9 and I10 are recorded rather than
resolved, and the I8 fix is a host edit made *after* the last inspection, so it
is unreviewed by the other provider. Both facts are the user's to act on.

### Host follow-up — I9 and I10 closed (unreviewed by the other provider)

Both were fixed after the inspection budget was spent, at the user's direction.
Neither has been through a Codex session; the proof is the host's own, below.

**GL037-I9 — gameplay actions bypassed the guarded path.** The cause reached
further than the two drivers named: `bot.do()` submitted without checking
whether we were in the world, and `look`, `turn`, `pitch_to`, `mine`, `key` and
`place` all called it. Every aiming helper in the file could therefore run a
whole loop against a pause menu, with each action accepted, executed and inert.
Those helpers now call `bot.act()`; `do()` remains for driving a menu
deliberately and says so. `wood.py` and `fell3.py` no longer hold private HTTP
helpers at all, and both stop rather than proceed when `resume()` fails.

**GL037-I10 — churn was being counted as blocks.** Replaced with a reading of
the nine hotbar slots: `bot.slots()` takes each slot's mean colour, and
`bot.picked_up()` reports the largest slot change *minus the median* slot
change, because the slots are semi-transparent and the world behind them drifts
across all nine together while an arriving item moves one.

Calibrated live, both directions:

| event | slot deltas | signal | verdict |
|---|---|---|---|
| mined one dirt block | max 20.9, rest ≤ 1.7 | 20.58 | picked_up **True** |
| 90° camera sweep, nothing taken | max 3.6 | 2.04 | picked_up False |
| 0.30s swing (too short to break anything) | — | — | picked_up False |

That first row is also the evidence for the finding: the swing that really did
collect a dirt block measured **churn 2.50**, below `dig.py`'s old threshold of
3.0. The churn counter would have scored a successful break as a miss.

`dig.py` keeps churn for *aiming* — a swing that moved nothing is pointed at
air, whatever it broke — and counts only from the hotbar. A missing inventory
reading is reported as unscored rather than as a miss. End-to-end run after the
change: 26 swings, one block collected and counted by the hotbar, no driver
touching HTTP directly.

`pytest tests/ -q`: 130 passed.

