# Independent review: remaining GameLens phases

Agent: independent_review; task 01a104c3-38f1-7284-a5f9-39ae6f6450e9.
Parent execution task: 01a104b7-d463-71b6-b39c-15dcba3760d0.
Requested worker model: gpt-6.1-sol; observed model unknown (read-only detector: rollout_not_found), neutral shared guidance.
2026-10-04 UTC. Status: in progress; source modules still arriving.
Scope: independent source review, evidence only. No source edits, commits, tests, live gameplay/input, settings, installation or publication. Primary owns serialized validation and any remediation. Obsidian sync pending; attributed log retained here.
Applicable instructions: model-aware-skills and lean-task-execution plus obsidian-task-log read. No AGENTS.md found in checked workspace ancestors or checkout.

## Initial findings sent to primary

- P1 app.py:333: LocalRunStore(root=...) keyword incompatible with base_directory constructor; every real GameLens construction raises TypeError. Suggested test: constructor fixture with mocked capture/safety proving default and opt-in initialization.
- P1 game_skills.py:317-322: RunBudget cancellation/deadline never reaches submitted executor sequence. Example: 2-second key hold returns pending at 0.5 seconds, runner stops uncertain while queued input continues; cancel during dwell does not release it or prevent next press. Existing executor Event belongs to global safety kill. Suggested test: cancellation after first press/during dwell yields bounded release and no subsequent press, with per-skill cancellation preserving unrelated actions.
- P2 app.py:493-498: crop encoding occurs after final live-backend validation; retirement during crop encode yields an image from a retired backend. Suggested test: monkeypatch crop encoder to retire backend then assert NO_FRAME.

## Review checkpoints

- Verified runtime RunMetrics() has no persistence sink; optional disk is checkpoint-only, so disk fsync does not run before arbiter dispatch. The standalone sink API remains synchronous and must not be attached on input-critical paths.
- Perception exact independent axis transforms, immutable full parent and existing guard routing inspected; no authority from diagnostic fresh flag.
- Learning proposals are immutable advisory snapshots; exact whole-proposal hash and matching authenticated owner are required to review/activate. HTTP operator_only used for review/activation, unlike existing session controls intentionally authorized for agents in base.
- Learning validation expanded by worker during review; waiting for coherent integration and serialized regression evidence before final disposition.

## Follow-up checkpoint (2026-10-04 UTC)

- Correction to initial cancellation example: app._dispatch waits wait + expected sequence duration, so a normal 2-second hold usually completes rather than becoming pending after 0.5 seconds. The in-flight cancellation issue remains: RunBudget.cancel/deadline is absent from executor Sequence, so cancellation during dwell is observed only after blocking dispatch returns. Worker has added preservation of receipts and cancellation checks after dispatch; this solves receipt loss but not cancellation of in-flight input. Primary notified.
- Constructor keyword fixed by primary. Primary also identified /skills calling a nonexistent catalog.to_dict and is repairing route. Coherent module baseline reported; focused initial log shows 177 passed, 1 skipped (symlink host permission), as run by primary, not reviewer.
- P1 reviewed_learning.py:_commit predictable .tmp opened in write mode follows preexisting symlink/reparse parents; optional persistence can truncate unrelated owner file. Runtime learning memory-only, so current HTTP endpoints are unaffected. Suggested test: preexisting temp symlink/parent link is refused and unrelated contents survive; unique exclusive temp cleaned on failure.
- P2 run_metrics.py:record_observation clamps future capture age to zero then may fresh=True. Suggested test captured clock+1 is explicitly untrustworthy/fresh=False.
- P2 perception_bridge.py:47-48 assumes x/y on every sequence click; parse_sequence permits coordinate-free click at current cursor. Suggested cropped sequence test preserves coordinate-free click while mapping move/click coordinates.
- Resource robustness note: learning HTTP helper buffers request.body before 64 KiB rejection. Suggested stream read accumulating bytes with early rejection.
- Cropped /act rebind inspected: mapping returns parent-image coordinates and uses preserved full parent's JPEG and Observation. No guard bypass found. Label anchors on cropped tokens refused. MCP missing crop metadata falls back to full-frame fetch before storing shown token; old runtime compatibility retained.
- Callback outcome correlation uses action IDs and persists terminal outcomes after pending response. Schema supports last_completed_step, after_frame and churn, but current callback does not supply them; primary notified to preserve available provenance.

## Remediation readback (2026-10-04 UTC)

Verified fixed in current source: LocalRunStore constructor keyword; /skills catalog serialization; post-crop backend retirement validation; coordinate-free sequence click preservation; future capture ages explicitly not fresh; streaming 64 KiB learning body limit; unique exclusive learning temporary file and symlink/reparse ancestor/destination checks before reading/writing.

Skills owner revised execution to monitor a daemon guarded callback and invoke existing selected-session safety.kill on cancellation, deadline, pending or callback error, permanently closing its adapter. Synthetic cancellation tests use the real supervisor cleanup and InputExecutor with SendInput fully stubbed. This deliberately latches the selected session off; restarting/recovering is an explicit operator action.

Follow-up findings sent to primary:
- P1 cancellation monitor initially used budget.remaining outside halt-covered try; deadline/cancel race could escape without killing active input. Readback now uses completed.wait(0.01), eliminating that extra outside budget check.
- P2 callback duration bound initially omitted parsed sequence duration, killing ordinary >0.75-second actions despite adequate skill timeout. Fix requested: duration+wait+overhead; prove a longer synthetic hold succeeds without kill.
- P2 terminal-to-pending ordering: initial outcome guard released lock before _event append, allowing terminal callback then late pending. Readback now checks pending under the same _event append lock; this race is resolved. Suggested deterministic barrier fixture retained with primary.

No remaining guard bypass identified. Review still in progress pending final stable source and root's serialized test evidence; reviewer has run no tests.

## Validation checkpoint (2026-10-04 UTC)

Primary's focused-integrated.log: 338 passed, 5 skipped, 1 failed. Failure was synthetic FakeFrames lacking latest_id for post-injection provenance; fixture now supplies the same method as the real capture slot. Runtime callback behavior was preserved.
Primary's review-regressions.log after fix: 110 passed, 4 skipped, 1 warning in 3.28 seconds. Four symlink creation checks skip because host Windows privilege 1314 prevents creating fixture links; exclusive-temp collision checks run. Source link/reparse checks inspected. No setting/privilege changes requested.
CRLF-aware whitespace validation (git -c core.whitespace=cr-at-eol diff --check) passes. Plain diff --check labels new CRLF lines trailing whitespace; this is newline format, not trailing spaces. No diff in arbiter.py, input.py, safety.py or capture.py.
Skills callback bound now includes parsed duration and callback thread completion is joined for at most 50ms before admitting next step. Valid longer-guarded-sequence and consecutive verified-step fixtures added. Earlier cancellation, in-flight cleanup, timer-race and normal-duration findings resolved by source readback.

Final lifecycle edge case sent to primary (P2): runtime stores one run_id for its whole process lifetime, but offline RunMetrics idle-age pruning expires that ID after 86400 seconds. Subsequent record calls return None and /metrics null indefinitely. Suggested runtime fake-clock regression: idle past age bound, resume encode/dispatch, verify a resumed valid run; use active-run retention or explicit rotation without changing authority. Awaiting primary disposition and stable final validation.
