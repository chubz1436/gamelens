# PLAN-GL038 review log (append-only)

- Started 2026-09-23. Host: Claude Code (Opus 5.5), coordinator and builder.
- Plan reviewer: Codex CLI `codex-cli 0.154.0`, requested model `gpt-6-astra`, effort high, read-only sandbox.
- Final inspector: fresh Codex session, same requested model.
- Plan: `PLAN-GL038.md`. Rounds: max 3 plan-review, 2 fix, 2 inspection. Inspection: on.
- Scope: render-lag wait (after_frame / frame.jpg?after), positive in-world probe, live GL-036 proof.
- Authorization: build yes ("improve mo pa ang gamelens"); commit/push no.
- Base: `82a75d2`. Pre-loop uncommitted edits in gamelens/capture.py and gamelens/app.py disclosed in the plan.
- Baseline: 132 tests passed.

## Plan review round 1 — REVISE
- Runner result: `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-r1/claudex-vruyd71x/result.json` (session 01a0cc6c-7656-79d3-8341-6dab1f7d30da, plan sha cd21d734…, 138 s, observed model: not reported).
- GL038-R1 (medium) calibration used requested k, but acquire returns newest qualifying frame → ACCEPTED, measurement redesigned on observed offsets with validation.
- GL038-R2 (medium) picked_up is not ground truth for a break → ACCEPTED, visual labels from saved target crops, hotbar corroborates.
- GL038-R3 (high) sync blocking /frame.jpg waits share the worker pool with /stop → ACCEPTED, async wait on the loop + admission 429 + /stop regression test.
- Limitations stated by reviewer: static only; offline calibration images and hb5.py not inspected.

## Plan review round 2 — REVISE
- Runner result: `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-r2/claudex-4wkm0ohl/result.json` (same session, plan sha 1c013633…, 74 s). Reviewer: R1–R3 addressed at plan level.
- GL038-R4 (medium) stream left on encode_latest cannot emit X-GameLens-Frame → ACCEPTED, stream migrated to encode_frame.
- GL038-R5 (medium) failover clear() between poll and encode would 503 early → ACCEPTED, three-way encode result, NO_FRAME resumes waiting under the original deadline.

## Plan review round 3 — APPROVED
- Runner result: `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-r3/claudex-vwzztsjy/result.json` (same session, plan sha 3fbd2034…, 40 s). No findings. Reviewer: "approves the revised plan, not implementation or live acceptance".
- Limitations: static only; calibration screenshots not independently inspected; model identity not reported by the CLI.
- Approval check run before build (below). Pre-build commit: 82a75d2 (with the disclosed uncommitted edits).

## Build (Claude) — 2026-09-23
- Files: gamelens/{app,capture,server}.py, bot.py, drivers/approach.py; new screens.py, tests/test_frames_after.py, tests/test_in_world.py, tests/fixtures/screens/*.jpg (10 frames, 195 KB, rows above 80% blanked).
- Suite: 132 → 179 passed.
- Mutation checks (each applied alone, then restored; all KILLED): acquire_at_least `<`→`<=`; drop `sent` guard on after_frame; drop the id>0 guard; encode_frame ignores min_frame_id (survived at first — only the fake runtime covered it — so direct encode_frame tests were added, then KILLED); NO_FRAME not waited through; no admission limit; stream header read from the slot instead of the encoded frame; in_world interior rule dropped / any column a side / dim pixels counted bright / no padding (survived at first → mirrored rightmost-slot fixture added → KILLED); resume() ignoring in_world (bot made testable with a stub `play` module → KILLED).
- R3 differential (scratch r3_diff.py, 45 concurrent waits, shared portal): pre-review design (sync route blocking 2 s) delayed /stop by **1703 ms**; shipped design answered /stop in **<16 ms** (below timer resolution) with the excess waits refused 429.
- Deviation from plan: fixtures blank everything above 80% height (in_world reads only the hotbar band) to meet the < 400 KB target; stated in the test module.

## Live verification — not run
- The Minecraft client was not running. Access to the Minecraft Launcher was requested through computer use and **denied by the user**, so the game was not started. Render-lag calibration (item 1), live in_world check (item 2) and the GL-036 real-block trial (item 3) are **not verified**.

## Inspection round 1 (fresh Codex session) — REVISE
- Runner result: `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-i1/claudex-pw7su4de/result.json` (session 01a0cc79-36e8-7552-a1af-703a7ffabd06, 237 s). Reviewer read all 20 changed/new files incl. the 10 fixtures, confirmed CRLF retained.
- GL038-I1 (medium) AFTER_FRAMES=2 enabled as a "measured" default without the calibration → ACCEPTED. Plan's step-5 path applied: AFTER_FRAMES=None, frame_after raises without explicit frames, approach.py passes frames=3 labelled as an assumption; test added. Suite 180 passed.

## Inspection round 2 (fresh Codex session) — APPROVED
- Runner result: `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-i2/claudex-0tjd00nv/result.json` (session 01a0cc7e-51b9-72d3-9f96-4d2fa1fbd97d, 152 s). No findings. "This approval does not establish live acceptance."
- Rounds used: plan review 3/3, inspection 2/2, fix rounds 1/2. Authorship: all code by Claude; inspected only by Codex. No commit, no push.

## Live verification — run after the Owner opened Minecraft (same day)
Server restarted on 127.0.0.1 (tokens to %TEMP%), fresh demo world, Continue Playing only.
- **Item 2:** in_world False on the title screen (out-of-sample) and on the pause menu opened by Escape; True in-world; resume() closed the menu and returned an observation.
- **Item 1:** noise floor p95 0.15 → T=4.0. 60 turn trials, one fetch each, by observed offset d: d=1 0/9, d=2 7/11, d=3 10/10, d=4 9/9, d=5 10/10, d=6 11/11 → n=3. Validation 20/20 at frames=3, median wait 62 ms (max 78). Changed frames scored ≥42.2, d=1 frames ≤0.12. Backend printwindow 59.5 fps (WGC had failed over at world load: the title changed and the identity check correctly refused it). `bot.AFTER_FRAMES = 3`; approach.py uses the default.
- **Item 3 (GL-036):** 5 short/full pairs, labelled by eye from saved frames (`C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl036live/`):
| hold | block | label (visual) | churn | hotbar `picked_up` |
|---|---|---|---|---|
| p0 short 0.30s | leaves | unresolved — "before" frame caught mid-pitch | 15.26 | no |
| p0 full 1.30s | leaves | **broke** (sticks dropped) | 21.94 | yes |
| p1 short | grass | intact (cracks, same block) | 0.85 | no |
| p1 full | grass | **broke** (dirt below; first dirt item) | 13.83 | yes |
| p2 short | dirt | intact | 1.94 | no |
| p2 full | dirt | **broke** (hole one deeper; dirt 1→2) | 13.88 | **no** |
| p3 short | dirt | intact | 0.84 | no |
| p3 full | dirt | **broke** (dirt 2→3) | 12.17 | **no** |
| p4 short | dirt | intact | 0.88 | no |
| p4 full | dirt | **broke** (dirt 3→4) | 6.83 | **no** |
  Confirmed broke 5, confirmed intact 4, unresolved 1. Every broke churn (min 6.83) > every intact churn (max 1.94) → acceptance met. Hotbar corroboration caught 2/5 breaks: stacking onto an existing stack only changes a digit.
- **Unreviewed edit:** after inspection round 2 approved, bot.AFTER_FRAMES went None → 3 (with its measurement comment), approach.py dropped its explicit frames=3, and the I1 test now asserts the default of 3 while still checking the no-default path. Inspection budget (2/2) was used up; these few lines were not seen by Codex. Suite 180 passed.

## Follow-up fixes (Owner: "ayusin mo ang mga dapat ayusin at e pa review mo kay codex") — spec FIX-GL038.md
- picked_up now also compares count-digit pixels (white + font shadow; ≥16 px in one slot, ≥3× any other). Live labelled pairs: 5/5 breaks detected (was 2/5); all no-change pairs False. Out-of-sample live: dirt 4→5 True; 2 full holds on stone (unbroken) and 2 short holds False. 4 mutations killed.
- play.HWND resolved from the server's /state instead of a stale constant; live: 329882, hold_focus True.
- Inspection F-round 1 (fresh Codex, `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-i3/claudex-rss4cxhk/result.json`): REVISE — GL038-F1 import-time crash on malformed /state → ACCEPTED, fixed, 20 tests.
- Inspection F-round 2 (fresh Codex, `C:/Users/CHUBZS~1/AppData/Local/Temp/claude/B--AI-Agent-folder-GAME-VIDEO/20d12316-c40d-48f3-ae5e-bc7d2232e694/scratchpad/gl038-i4/claudex-a09rsrfe/result.json`): **APPROVED**, no findings; covers the whole diff from 82a75d2 including the formerly unreviewed AFTER_FRAMES edit.
- Suite 206 passed. Deviation: fixtures 425 KB (> 400 KB target). Not committed.
