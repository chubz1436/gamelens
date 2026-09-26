# PLAN-GL039 review log (append-only)

- Host / planner / builder: Claude Code, Claude Opus 5.5 (this session).
- Plan reviewer: Codex CLI `codex-cli 0.154.0`, requested model `gpt-6-astra`, effort high, read-only sandbox.
- Final inspector: fresh Codex session, same requested model.
- Scope: PLAN-GL039.md (agent control surface + probe-gated background input).
- Authorization: build yes; commit/push no.
- Limits: 5 plan-review rounds, 2 fix rounds, 2 inspection rounds. Inspection on.
- Inspection base: ad8984a.

## Round 1 (attempt 1) — BLOCKED, operational

- Result: scratchpad/r1/claudex-js0sky8c/result.json, session 01a0cf1e-2f64-7a31-be76-bc15dd5cff96, plan sha 345f6d9b…, 43 s, observed model: not reported.
- Cause: to keep write-capable MCP servers (pisowifi, adobe_premiere, node_repl, chubz_readonly, chubz-olt) out of the reviewer, Codex ran with a private CODEX_HOME holding only auth + model settings. That dropped `[windows] sandbox = "elevated"`, so every read-only shell command was refused. Not an approval; not counted as a completed review.
- Finding still raised, from the plan text alone:
  - GL039-R1 (high): MCP act silently rebinds a click to an unseen frame. **Accepted.** A3 rewritten: clicks bound to the shown observation; rebinding on STALE/EXPIRED only if the fresh frame is the same screen (same size, thumbnail MAD ≤ 2.0); otherwise no dispatch and a fresh image. Non-coordinate actions rebind to a fresh frame with the trade-off stated and a `strict` opt-out. Tests (a)–(f) and a live threshold calibration added.
- Fix for the run: `[windows] sandbox = "elevated"` added to the private CODEX_HOME (still no MCP servers). Fresh review session started (a BLOCKED session is not resumed).

## Round 1 (attempt 2) — REVISE

- Result: scratchpad/r2/claudex-wcnditro/result.json, session 01a0cf1f-d1ae-7290-a729-ec8e7a32be80, plan sha ae4761ae, 195 s, observed model: not reported. Coverage: plan, arbiter, input, safety, server, app registry, agent reflex preemption, capture fallback, screens, tests, README.
- GL039-R1 (high) Alt-down/Tab-tap in a sequence is Alt+Tab. **Accepted.** Sequences use SEQUENCE_KEYS (letters, digits, space, shift, ctrl, arrows); alt/tab/escape/enter/F-keys refused in sequences; shortcut tests.
- GL039-R2 (high) client-side rebinding launders preemption/session/geometry when age also failed. **Accepted.** Rebinding is server-side (`/act` `rebind: true`): shown record must exist (fail closed); target, session (not retired), geometry generation, preemption counter and size must match; combined stale+preempted test.
- GL039-R3 (high) a global 64x36 MAD cannot authorise a click. **Accepted.** Click rebinding needs a 33x33 colour patch at the click point unchanged (max 12, mean 1.0) plus global colour MAD 2.0; the global score never authorises the target; residual risk stated; localized and colour-only change tests.
- GL039-R4 (high) window mode + MSS captures the covering window. **Accepted.** Window mode refuses `--backend mss`, fallback stops at PRINTWINDOW, guard denies CAPTURE_UNSAFE on MSS; probe refuses on MSS.
- GL039-R5 (medium) probe cases not isolated. **Accepted.** Least-disruptive order, per-case prechecks, up-messages in finally, restore check; failures make later cases inconclusive.

## Round 2 — REVISE

- Result: scratchpad/r3/claudex-4o1bzkne/result.json, same session 01a0cf1f-…, plan sha 2306d113, 138 s. R1–R5 confirmed addressed.
- GL039-R6 (medium) B1 can score MSS frames after a mid-hold failover. **Accepted.** B1 requires a forced wgc/printwindow backend (no failover by construction, capture.py:791,879) and compares /state backend + session_id around each case; any change → inconclusive; unit test with a session change behind a hotbar-visible frame.

## Round 3 — REVISE

- Result: scratchpad/r4/claudex-gylke2cr/result.json, same session, plan sha 0eb597ba, 51 s.
- GL039-R7 (medium) /state omits session_id and the forced backend, so the R6 check has nothing to read. **Accepted.** stats() gains forced_backend; state() passes session_id and forced_backend through; probe refuses on missing/null/mss; tested through the real state()/HTTP path.

## Round 4 — APPROVED

- Result: scratchpad/r5/claudex-ajxeiv5t/result.json, same session, plan sha c314e5167f7710bbb07d707ebbe69e2530aeb63c3fd46691434ae33dbe5e31eb, 25 s, observed model: not reported. `runner check`: "Approval matches the current plan."
- Reviewer limitations: plan only; live calibration and background-input capability unverified.
- Rounds used: 4 completed of 5 (plus one BLOCKED operational attempt).

## Build

- Builder: Claude (host). Pre-build commit: ad8984a.
- Built by Claude (host) directly. Part A (A1 sequence, A3 MCP + server-side rebind) and part B1 (probe) built; B2 NOT built: it is conditional on B1's live result, and B1 needs the Owner's game running (it is not).
- Files: gamelens/arbiter.py (SEQUENCE_KEYS, parse_sequence, sequence_action, freshness-at-first-press, is_fresh, rebind_rejection), gamelens/app.py (registry keeps JPEG+quality, same_at_click, _bind, submit_sequence, rebind on every submit, /state session_id+forced_backend), gamelens/server.py (kind sequence, rebind bool), gamelens/safety.py (rate_capacity), gamelens/capture.py (forced_backend in stats), gamelens/mcp.py (new), tools/bg_input_probe.py (new), .mcp.json, README section; tests: test_sequence, test_rebind, test_http_sequence, test_mcp, test_bg_probe (new), test_frames_after (fake registry signature only).
- Proof: `.venv/Scripts/python.exe -m pytest tests/ -q` → 350 passed (207 before).
- Mutation check: 20 mutants over the new rules, all killed. First run: 3 survived — two exposed weak tests (colour-only fixture whose grey diff tripped on JPEG edge ringing; no test where only the frame/record session comparison could refuse), fixed by strengthening the fixture and adding the test; one was a no-op mutant of mine, replaced.
- Live, dry-run (GameLens never armed; nothing injected): real `python -m gamelens --backend printwindow` + real `python -m gamelens.mcp` against windows owned by the test. Static window: click 2 s after look → bound_to fresh, patch_max 0; strict → STALE_OBSERVATION; Alt+Tab sequence → 400; after 6 s → registry STALE, fail closed; /state shows forced_backend printwindow, session_id 1. Moving window: click → SCREEN_CHANGED, patch_max 255; key → rebound fresh.
- Not done (needs the Owner's game): in-world sequence check, patch-threshold calibration on real menus, B1 probe run.

## Inspection 1 — REVISE (fresh Codex session)

- Result: scratchpad/i1/claudex-nkztlk8v/result.json, session 01a0cf39-0f8b-71d0-beb2-4d347e5cb4f3, 260 s, observed model: not reported. Coverage: all 17 manifest files (SHA-matched), HEAD = base, traced sequence/rebind/probe paths. Limitations: static only.
- I01 (medium) MCP: a failed follow-up frame after a sent action surfaces only as "not reachable", inviting a duplicate. **Accepted.**
- I02 (medium) MCP: `strict`/`see_after` not type-checked; "true" silently means rebind. **Accepted.**
- I03 (medium) MCP: non-object params / non-string tool name kill the process. **Accepted.**
- I04 (medium) probe: after-frame threshold taken before injection. **Accepted.**
- I05 (medium) probe: failed restore still certifies E. **Accepted** (my test encoded the wrong rule).
- I06 (medium) probe: foreground checked only before the case. **Accepted.**
- I07 (medium) probe: whole-image churn vs one early baseline cannot attribute motion to the input. **Accepted.** Per-case no-input control of equal length, repeated trials that must agree, centre-region evidence for the mining hold.
- I08 (low) sequence auto-release grouped by device, not press order. **Accepted.**

## Fix round 1 (Claude, host)

- I01: `_look_safely` keeps the /act answer; a failed picture after a sent action says "WAS SENT ... do not repeat", isError false. I02: `_flag` requires real booleans for strict/see_after. I03: method must be a string (-32600), params an object (-32602), tool name a string; serve() also catches anything else as -32603 and keeps serving.
- I04: after-frames counted from a frame-id marker read from /state *after* the release. I05: an unverified restore makes E inconclusive (my original test had encoded the wrong rule; corrected). I06: foreground watched at every post and every 20 ms slice of every sleep; any sighting makes the case inconclusive. I07: each pixel case runs a same-length no-input control first; a control above MARGIN is inconclusive ("scene moved on its own"); effect must beat control + MARGIN on every one of 2 trials; the mining hold also needs centre-fifth churn > MARGIN and > 2x whole-frame churn (a camera turn moves both equally). I08: one ordered `held` list across keys and buttons.
- Found while fixing, test-only: two races in my executor test (preempting before W was down; checking releases before `_run`'s release_all, which by existing design runs just after the outcome is reported). Now bounded waits; 8/8 stable.
- Mutation re-run: 29 mutants (incl. one per I-finding), all killed, twice. Three survivors on the way exposed weak tests (a second trial's precheck masking a missing postcheck; `centre >= whole` passing a uniform change; I03 tests accepting any error code) — each test strengthened, and the crosshair rule tightened.
- Proof: 371 passed. Live dry-run MCP recheck on the static window: same results as before.

## Inspection 2 — REVISE (fresh Codex session) — inspection budget now exhausted

- Result: scratchpad/i2/claudex-u7stv0u3/result.json, session 01a0cf4c-7442-73b2-bf04-c807b145029f, 186 s. I01–I08 not re-raised.
- I09 (medium) probe: absence of a persistent change is scored "unsupported", but a 1.3 s hold on wood (3.6 s) or W into a wall changes nothing even when input works. **Accepted.** Pixel cases can only be supported or inconclusive; "unsupported" is kept only for E, whose stimulus always has a visible result when accepted. Left hold also checks the crosshair region mid-hold (cracks animate while held).
- I10 (medium) probe: preconditions not re-checked after the control interval, right before injecting; focus sightings only recorded. **Accepted.** Re-check on the control-end frame; new-input posts refuse once focus is seen; releases always go.
- I11 (medium) MCP: "WAS SENT" also for dry/pending. **Accepted.** Message follows the actual outcome.
- Fix round 2 by Claude. **Not independently inspected** (MAX_INSPECTION_ROUNDS = 2 reached); reported to the Owner as such.

## Fix round 2 (Claude, host) — NOT independently inspected

- I09: pixel cases are supported or inconclusive only ("no change attributable to the input; not proof it was ignored"); E alone may be unsupported. Left hold samples a frame mid-hold and accepts crosshair-local change there or after.
- I10: after the control interval the probe re-runs the postcheck (session, focus seen) and requires in_world on the control-end frame before injecting; `_post` refuses new input once focus has been seen, releases always go. (A separate explicit foreground re-check was removed as redundant after mutation showed it could never be the deciding check.)
- I11: the "picture failed" message follows the executor outcome: sent / dry (not injected) / pending (undecided).
- Proof: 378 passed. Mutation: 35 mutants, all killed (survivors on the way: two weak tests strengthened, one redundant check removed, one mutant pattern corrected).
- Inspection budget (2) exhausted: fix round 2 has had no fresh Codex inspection. Offered to the Owner.

## Final state

- Rounds: plan review 4 completed (+1 BLOCKED operational); inspections 2 (both REVISE, 8 + 3 findings, all accepted and fixed); fix rounds 2.
- Unresolved findings: none known. Unreviewed edits: fix round 2 (I09–I11).
- Not done, needs the Owner's game: in-world sequence check, patch-threshold calibration on real menus, B1 probe run; B2 deferred by design.
- Not committed (no authorization to commit).
