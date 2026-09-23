# PLAN-GL038 — See the world after the action, know you are in it, and prove GL-036 on a real block

Three open items from the GameLens vault (`AI_PROJECTS_MEMORY/Projects/GAMELENS/Open Items.md`):
"Render lag is not capture lag", "The harness has no positive test for being in the world",
and GL-036's "Still outstanding: never verified against a real game breaking a real block".

Authorization: the Owner asked to improve GameLens ("improve mo pa ang gamelens") and to run it
through /claudex-route and /claudex-loop. Build is authorized; **commit/push is not** — the diff
is left for sign-off.

## Disclosure: work that predates this plan

Before the loop started, a first cut of item 1 was already edited into the working tree
(uncommitted, on top of HEAD `82a75d2`):

- `gamelens/capture.py` — `LatestFrame.latest_id()` and `LatestFrame.acquire_at_least(min_id, timeout)`.
- `gamelens/app.py` — `Dispatch.after_frame` (+ `to_dict`), `_dispatch` records
  `capture.frames.latest_id()` inside `on_outcome`, `encode_latest(quality, *, min_frame_id, wait)`.

The inspection base is therefore `82a75d2`, not the current working tree, so those lines are
reviewed with everything else. (An editor round-trip had also converted `app.py` from CRLF to
LF; that was reverted — the file is CRLF again and `git diff --stat` shows only the real lines.)

## Item 1 — render lag: wait for frames, not for guessed milliseconds

### Problem
`/act` answers when input is injected. A `/frame.jpg` fetched immediately afterwards can still
show the pre-action world — the game renders a frame or more behind its input. Today every
consumer guesses: `bot.SETTLE = 0.12`, `drivers/approach.py` sleeps `bot.SETTLE`,
`drivers/chop.py` sleeps 0.12–0.7s. A guess is either too long (slow) or too short (wrong).

### Contract
- `Dispatch.after_frame: int | None` — the id of the newest published frame at the instant the
  executor reported the outcome. Set only when the outcome is `sent`; `None` for denied,
  dry-run, pending, or when no frame had been published (`latest_id()` is 0 → report `None`,
  not 0). Returned by `/act` as `"after_frame"`.
- Frame ids come from the process-global `capture._frame_ids` counter, assigned when a frame
  is accepted for publication (after dedupe and pool lease), so they are strictly increasing
  across backend restarts and retargets. A frame with id > `after_frame` was accepted after the
  input was injected. It is **not** a promise the game has drawn the result — that is what
  `frames` is for.
- `GET /frame.jpg?after=A&frames=N&wait_ms=W`:
  - `after` optional int ≥ 0. Absent → today's behaviour (newest frame, no waiting).
  - `frames` int, default 1, 1 ≤ N ≤ 30. The served frame has id ≥ A + N.
  - `wait_ms` int, default 500, 0 ≤ W ≤ 2000. Out-of-range values → 422 (FastAPI `Query`
    bounds), never a silent clamp.
  - Timeout → **504** with `X-GameLens-Latest-Frame: <newest id>` and a detail naming A+N.
    Never a stale frame: the caller asked to get past it. (Distinguishes from 503 "no frame at
    all".) A caller that knows its backend only publishes on change (WGC on a still screen) can
    read the latest-frame header and decide; the server does not decide for it.
- Every `/frame.jpg` response (with or without `after`) and every `/stream.mjpg` part gains
  `X-GameLens-Frame: <frame id>`, so a caller can see which frame it was given and chain.
- Implementation: `GameLens.encode_frame(quality, *, min_frame_id=None)` **never blocks** and
  returns one of three results (review R5): `Encoded(jpeg, observation_id, frame_id)` for the
  newest frame when it qualifies; `NO_FRAME` when the slot is empty or its id is below
  `min_frame_id` (including a `clear()` during failover); `ENCODE_FAILED` when a frame was
  leased but encoding raised. The `frame_id` is read from the same leased `Frame` the JPEG was
  encoded from, never re-read from the slot. `encode_latest` stays as a thin wrapper with its
  current 2-tuple return for callers that want it. `GameLens.latest_frame_id()` exposes
  `capture.frames.latest_id()`.
- `/stream.mjpg` **is migrated** to `encode_frame` (review R4) and emits
  `X-GameLens-Frame` from the `Encoded` result, so the header names the frame that was encoded
  even if a newer one publishes during the encode. Its dedupe key becomes the frame id.
- **The wait holds no worker thread** (review R3). `/stop` is a sync endpoint served from the
  same AnyIO worker pool as every other sync route and dependency, and its latency is a safety
  property; a blocking `acquire_at_least` inside a sync `/frame.jpg` would let authenticated
  callers with unreachable `after` values tie those workers up. So `/frame.jpg` becomes
  `async def`:
  - With `after`: first **admission** — a server-level counter of in-flight frame waits, max
    `MAX_FRAME_WAITS = 4`; a request beyond that gets **429** immediately, before any waiting.
    Then the wait runs **on the event loop**: `while runtime.latest_frame_id() < target and
    now < deadline: await asyncio.sleep(0.004)` (a ~4 ms poll of a lock-guarded int; ≤ 4
    pollers). No thread is held while waiting. Timeout → 504 as above. The counter is
    decremented in `finally`.
  - Then the encode runs via `await asyncio.to_thread(runtime.encode_frame, quality,
    min_frame_id=target)` — the pattern `/stream.mjpg` already uses — so the only worker time
    used is the encode itself. If the qualifying frame is superseded between poll and encode,
    the newer frame still satisfies `id ≥ target`. If the encode returns `NO_FRAME` (the slot
    was cleared by failover between poll and lease), the handler **resumes polling with the
    same deadline and the same admission slot**, and answers 504 only when that deadline
    expires. `ENCODE_FAILED` → 503 "frame encode failed" immediately (a retry would re-encode
    the same frame). Without `after`: `NO_FRAME` → 503 "no frame available yet" (today's
    behaviour), `ENCODE_FAILED` → 503 "frame encode failed".
  - Regression (R5): a fake runtime whose `latest_frame_id` reports a qualifying id but whose
    first `encode_frame` returns `NO_FRAME` and second returns a frame → 200 with that frame;
    and one that keeps returning `NO_FRAME` → 504 after `wait_ms`, not 503.
  - Regression (R4): a `/stream.mjpg` part's `X-GameLens-Frame` equals the id of the frame the
    fake encoded, while the fake's `latest_frame_id` already reports a newer id.
  - `LatestFrame.acquire_at_least` stays (used by unit tests and in-process callers with a
    timeout) but no HTTP path blocks on it.
- Regression check for R3: with the wait-admission full (4 requests waiting on an unreachable
  `after`, `wait_ms=2000`) and 20 more rejected with 429, `POST /stop` completes in < 0.5 s.
  Mutation: make `/frame.jpg` a sync route that blocks in `acquire_at_least` with no admission
  limit and fire 45 concurrent waits → the same `/stop` assertion must fail (AnyIO's default
  pool is 40).
- Retarget/`clear()` while waiting: `acquire_at_least` keeps waiting for a frame whose id
  reaches the target; ids keep increasing across sessions, so a frame from the new session
  satisfies it. That is correct: it was captured after the action.

### bot.py
- `frame_after(r, frames=2, q=50, wait_ms=500)` — given an `/act` result, fetch
  `/frame.jpg?after=<after_frame>&frames=<n>`; returns `(arr, obs)` or `(None, None)` when
  `after_frame` is null or the server answers 504/503. No sleep fallback: missing evidence is
  reported as missing.
- `SETTLE` stays for callers not yet migrated, with its comment pointing at `frame_after`.
- `drivers/approach.py` (the one driver that sleeps `bot.SETTLE` before reading pixels) moves to
  `frame_after`. Other drivers' sleeps are GUI pacing, not render waits; out of scope.
- The default `frames` for the bot is set from the live measurement below, not guessed.

### Live measurement (acceptance) — revised per review R1
`after`/`frames` returns the *newest* qualifying frame, so the requested `k` is not the offset
observed. The calibration therefore records what was actually served:

1. **Noise floor.** With the player idle, fetch 30 consecutive frames (`after=<last id>`,
   `frames=1`) and compute the change metric between successive ones. Metric: mean absolute
   difference of the centre 40% × 40% crop, greyscale, both frames decoded from the served JPEGs
   at the same quality. Threshold `T = max(4.0, 3 × p95(idle diffs))`.
2. **Trials.** For each trial: fetch the pre-action frame P (its id from `X-GameLens-Frame`),
   `/act look dx=±400` (alternating sign), then for `k` in a trial-specific single value drawn
   round-robin from 1..6, one `/frame.jpg?after=F&frames=k` — **one fetch per trial**, so no
   trial's later request is contaminated by an earlier one. Record `F`, `k`, the served id `S`,
   the observed offset `d = S − F`, elapsed ms, and `changed = metric(P, S) > T`.
3. **Estimate.** Group by observed `d` (not `k`). A `d` value is usable only with ≥ 5 trials.
   Candidate default `n` = the smallest `d` such that every usable bin ≥ `d` has ≥ 95% changed.
   Offsets never observed are reported as unobserved, not interpolated.
4. **Validate.** 20 fresh trials with `frames=n`: report the changed rate. `n` becomes
   `bot.frame_after`'s default only if the validation rate is ≥ 95%.
5. **If nothing qualifies** (no `d` ≤ 6 reaches 95%, or validation fails): the default is not
   changed, `frame_after` requires the caller to pass `frames`, and the vault records the table
   and that render lag was not bounded in frames on this machine.
Trials: ≥ 60 in step 2. Backend and publish rate are recorded alongside.

## Item 2 — a positive test for "I am in the world"

### Problem
`resume()` decides "in the world" from three negatives (`menu_open`, `demo_dialog`, `dead`
not checked there at all). A settings screen passes all three, so actions go into a menu and
read as a still screen.

### Probe: the selected-slot outline
Every modal screen the demo has shown — pause menu, demo dialog, death screen, inventory,
crafting table, Options, title, "are you sure" — either removes the HUD or draws it dimmed and
blurred under a darkening overlay. The one thing only a live, un-overlaid world draws is the
**selected hotbar slot's outline**: a bright, unsaturated box one slot wide whose two vertical
sides span the bar's height.

`in_world(arr)`: within the existing `HOTBAR` fractions (`0.2885, 0.898, 0.7057, 0.979`),
padded 2% of width left/right, take pixels with V > 190 and S < 60 (HSV). A column is a *side*
if ≥ 50% of the bar's rows are bright in it. True iff some pair of side columns is
0.85–1.40 slot-widths apart **and** the columns strictly between them average < 0.4 bright
(rejects a solid bright block). No hearts check: hearts measure health, not presence (low-health
frames have one heart).

Calibration already run offline against every saved real frame from the last two demo sessions
(scratchpad, 870×519 and 856×512 windows; script `hb5.py`): **all** in-world frames positive
(including 1-heart, night, underground, torch-lit, F3 overlay, full hotbars with count digits
over the outline); **zero** positives among pause menu ×~30, demo dialog ×~15, death screen ×4,
inventory/crafting ×~25, Options, title, "are you sure you want to quit". Two thresholds were
tuned on that set (padding, 0.5 coverage because count digits occlude the right side); that is
in-sample, so a small fixture set becomes the regression test (below) and the live check is
the out-of-sample one.

### resume() change
After the existing demo-dialog / pause-menu handling, `resume()` returns an observation only
if `in_world(arr)` is True. If the screen is none of: in-world, pause menu, demo dialog, it
returns `None` **without sending any input** — no Escape, no click — because the screen is
unknown and every input to an unknown screen is how View Bobbing got toggled. The final
post-recovery check likewise requires `in_world`. `act()` already turns `None` into a
`denied`-shaped result with "could not resume the world".

`bot.inventory()` / `picked_up` unchanged.

### Tests
The repo tests are harness tests with no Minecraft fixtures. Add `tests/fixtures/screens/` with
~8 small real frames (cropped to the bottom 30% + full-size copies downscaled to keep the repo
small — target < 400 KB total): in-world (day, night-underground, 1-heart, full hotbar), pause
menu, demo dialog, death screen, Options. `tests/test_in_world.py` asserts the classifier on
each, and that a synthetic solid-bright band is rejected.

`bot.py` cannot be imported by tests: it imports `play` and reads live tokens at import time.
So the pure pixel probes (`grey_slab`, `at`, `menu_open`, `demo_dialog`, `dead`'s pixel part,
`in_world`, `slots`, `picked_up`, `HOTBAR`, `SLOT_CHANGE`, the button fractions) move into a new
side-effect-free `screens.py` at the repo root, and `bot.py` imports them under the same names so
every driver keeps working unchanged. `dead(arr=None)` keeps its fetch-if-None wrapper in
`bot.py`. Tests import `screens` directly.

## Item 3 — GL-036 on a real block

Run in the live demo world (the second Minecraft client belongs to the user and is not touched;
"Purchase Now!" is never clicked; `dismiss_demo_dialog` is the only thing that clicks that
dialog).

Protocol (revised per review R2 — the hotbar is supporting evidence, not ground truth):

- Setup: empty or known hotbar state recorded; player standing so the target dirt block is
  adjacent at the crosshair; no loose drops nearby (checked on a saved frame).
- Each **trial pair** uses one fresh dirt block B: first a short hold (0.30 s), then a full hold
  (1.30 s; dirt barehanded breaks at 0.75 s) on the same B, both `measure=true`.
- For each hold, save: the pre-action frame, the post-action frame via `frame_after` (item 1),
  and a crop of the crosshair region (the targeted block face) from both. Also record
  `outcome`, `churn`, `after_frame`, `picked_up(before, after)`, and — for full holds — a second
  hotbar reading 1.5 s later (bounded collection window).
- **Label** each hold `broke` / `intact` / `unresolved` by **direct visual inspection of the
  saved before/after target crops** (the block face is gone and the view shows what was behind
  it, or it is still there). The hotbar reading is recorded as corroboration; a disagreement
  between hotbar and visual label is reported, and a hold whose crops are ambiguous is
  `unresolved`. A short hold that shows a hotbar change is noted as a possible late collection
  of an earlier drop.
- Only holds with outcome `sent` and a `broke`/`intact` label enter the comparison.

Acceptance is **recording the truth**, not a pre-chosen result:
- GL-036's checkbox is ticked only if there are ≥ 4 confirmed `broke` full holds and ≥ 4
  confirmed `intact` short holds, and every confirmed-broke churn exceeds every
  confirmed-intact churn. The table and the saved crops' paths go in the vault.
- If churn overlaps, the vault records that churn does not separate a break from a failed hold
  on live terrain and that mining effect must be judged from the target view / hotbar; the
  checkbox stays open with the evidence.
- If fewer than 4 + 4 labels are confirmed (demo ended, block unreachable, ambiguity), the item
  is reported as not verified.
## Non-goals
- No change to the arbiter's freshness rule, the executor, or capture backends' publish logic.
- No migration of the GUI-pacing sleeps in `craft_kit.py`/`chop.py`.
- No commit, no push (awaiting the Owner).
- Not solving other GUI scales / window sizes: `HOTBAR` fractions were measured at ~860×515
  with the demo's auto GUI scale. Stated as a limitation in `screens.py`.

## Risks
- WGC publishes only when the window content changes. After an action that changes nothing
  (or into a paused world), `after=` waits out `wait_ms` and returns 504. That is the honest
  answer; `frame_after` returns `(None, None)` and callers treat it as "no evidence".
- Frame ids are assigned on acceptance in our process, not at DWM composition; a frame composed
  just before injection but delivered just after could carry a larger id. `frames ≥ 2` absorbs
  this; the live measurement quantifies it.
- The in-world probe is in-sample-tuned; the fixtures pin it and the live run exercises it.
- Live items depend on the demo world state (player was sealed underground at the end of the
  last session; demo time may be short). If the demo expires, items 1 and 3 are reported as
  not verified, not assumed.

## Verification
1. `.venv/Scripts/python -m pytest -q` → all pass; count = 132 + new tests.
   New unit tests (in `tests/test_frames_after.py` and `tests/test_in_world.py`):
   - `acquire_at_least` returns immediately when the newest id already qualifies; blocks and
     returns the first qualifying frame published from another thread; returns None on timeout
     and never a lower-id frame; timeout ≤ 0 does not block.
   - `latest_id` is 0 before any publish.
   - `_dispatch` sets `after_frame` to the id current when `on_outcome` fired for `sent`, and
     `None` for denied / pending / no frame published.
   - HTTP: `/frame.jpg` without `after` carries `X-GameLens-Frame`; with `after` returns a frame
     with id ≥ A+N; 504 + `X-GameLens-Latest-Frame` on timeout; 422 for `frames=0`,
     `frames=31`, `wait_ms=-1`, `wait_ms=2001`; `/act` JSON contains `after_frame`.
   - `in_world` fixtures as above.
   - Mutation checks (manual, recorded): flip the `<` in `acquire_at_least` to `<=`, remove the
     `sent` guard on `after_frame`, drop the interior < 0.4 rule — each must fail a test.
2. `git diff --stat 82a75d2` shows only intended files; `app.py`/`capture.py` keep CRLF
   (`git diff --ignore-cr-at-eol --stat` equals `git diff --stat`).
3. Live (GameLens server restarted on 127.0.0.1 with tokens in %TEMP%): render-lag table
   (item 1), `in_world` true in-world / false on the pause menu reached by Escape (item 2),
   GL-036 table (item 3). Screenshots saved to the scratchpad; results written to the vault
   (`Open Items.md` + a dated session note).
4. Fresh Codex inspection of `git diff 82a75d2` (plus untracked new files).
