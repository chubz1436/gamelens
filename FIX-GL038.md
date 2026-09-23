# FIX-GL038 — follow-ups after PLAN-GL038's live run

PLAN-GL038 was Codex-approved (plan sha 3fbd2034…) and its implementation passed a fresh Codex
inspection. Afterwards the live run was done and produced three follow-ups. The Owner asked:
"ayusin mo ang mga dapat ayusin at e pa review mo kay codex" — fix what needs fixing, then have
Codex review it. This file is the spec for that review. Base for the whole diff remains `82a75d2`;
the parts new since the last approved inspection are listed below.

## 1. `bot.AFTER_FRAMES = 3` (unreviewed until now)
Inspection GL038-I1 required that no default be set without the plan's calibration. The
calibration was then run live: 60 turn trials grouped by observed offset (+1: 0/9, +2: 7/11,
+3..+6: 40/40), validation 20/20 at +3, median wait 62 ms. So `AFTER_FRAMES` went `None → 3`,
`drivers/approach.py` uses the default, and the I1 test asserts the default and still checks that
`frame_after` raises when no default exists. Evidence: `PLAN-GL038-REVIEW-LOG.md`, "Live
verification".

## 2. `picked_up` misses items that stack (new defect, found live)
Live GL-036 trial: 5 real breaks; the colour-only `picked_up` caught 2. The other 3 were dirt
landing on an existing dirt stack, which changes only the count digit.

Fix (`screens.py`): `slots()` returns a `SlotReading(colors, digits)`. `digits` holds per-slot
masks of count-digit pixels: white (V > 225, S < 35) **with** Minecraft's font shadow (V < 90) one
GUI pixel down-right (`k = round(w/430)`). `picked_up` is True if the colour test passes, or if
exactly one slot's digit mask changed by ≥ 16 pixels **and** ≥ 3× the next-largest slot's change.
A bare colour array (the old return type) still works, with the colour test only. `None` still
means no evidence.

Calibration evidence: every live pair was labelled by eye; the 5 breaks give 48/56/20/60 changed
digit pixels in one slot and 0 elsewhere (the fifth was a new item, caught by colour); every
no-change pair on record reads 0, except one older series whose frames sit lower in the capture so
slot borders enter the band (max 15 in one slot, 8 in another — refused by the dominance rule).
Without the shadow requirement, pale icons produced 7–27 px of noise per frame. Out-of-sample live
check after the fix: 1 real dirt break (count 4 → 5) → True; 2 full holds on stone (unbroken,
count unchanged) and 2 short holds → False. 5/5.

Tests: `tests/test_in_world.py` — real pickup pairs (1→2, 2→3, new item), no-change pairs (short
hold, same-inventory series), backward compatibility. Mutation-checked: digits ignored, no shadow
requirement, no dominance rule, threshold too high — each fails a test.

Known limit: a count that changes between two glyphs sharing most strokes (e.g. 8 → 9 or 1 → 7
at small GUI scale) could fall under 16 px. Not observed; stated, not solved.

## 3. `play.HWND` was a stale constant (defect, found live)
`play.py` hard-coded `HWND = 5442666`, a handle that dies with its window; after the game was
restarted `bot.hold_focus()` aimed at nothing (the live run had to override it by hand). Now
`play.target_hwnd()` asks the running server's `/state` for `target.hwnd` at import; 0 (take no
focus) when the server is down or the answer is malformed. Verified: server down → 0; server up →
329882, the real window, and `hold_focus()` → True. `mcplay.py`, an older standalone driver with
the same constant, is out of scope and unchanged.

## Verification
- `.venv/Scripts/python -m pytest -q` → 186 passed.
- Line endings: every edited file keeps its original endings.
- Fixtures: 20 real frames, 425 KB (the plan's 400 KB target is exceeded by 25 KB — digit shadows
  need JPEG quality 80).
- Commit/push: not done; awaiting the Owner.

## Round 1 inspection (fresh Codex) — GL038-F1, accepted and fixed
`target_hwnd()` crashed at import on a 200 whose JSON was `null`/list/string, or a `hwnd` of
`1e400`. Now: any non-200, undecodable body, non-dict state or target, or a `hwnd` that is not a
real int in (0, 2**64) — bools, strings, floats, negatives, zero included — returns 0. Missing
token files and a refused connection return 0. `tests/test_play_target.py` covers each (20 cases,
on the real `play` module with `req`/`tokens` replaced). Suite: 206 passed.
