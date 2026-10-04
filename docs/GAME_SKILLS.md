# Reusable game skills

`gamelens.game_skills` is an inert catalog and a local trusted-host framework. Importing it, registering definitions, or listing the catalog sends no input, launches no game, and changes no session. There is no HTTP/MCP execution endpoint. Existing Marathon/client opening/client closing sources are referenced; their routes, launcher scripts, and authentication procedures are not copied or automatically invoked.

`SkillCatalog.reference_catalog().list_skills()` returns three versioned reference-only entries. `get(skill_id, version)` selects an exact version; registering an existing ID/version is refused. Each entry supplies schema_version=1, semantic version, explicit game/profile compatibility, prerequisite checks, outcome checks, workflow references, bounded timeout and observation budget. `definition_hash` is SHA256 of the canonical full definition. Definitions and observation facts are detached immutable snapshots; listing returns detached JSON.

A `SkillDefinition.from_dict()` may contain at most 32 explicit `SkillStep` decisions. Every step requires preconditions and an expected outcome. Checks compare a named observed fact to a JSON scalar with exact type; missing or truthy-but-mismatched facts fail. Unknown definition fields, unsupported schema, omitted explicit schema, missing verification/compatibility, nonfinite timeout and out-of-range bounds are refused. Timeout is at most 3600 seconds; at most 64 observations can be consumed per run. These ceilings are framework limits, never permission to change existing guard limits.

Trusted host interface:

```python
catalog = SkillCatalog.reference_catalog()  # read-only source references
executor = GuardedActionExecutor(existing_gamelens)
runner = SkillRunner(observe, executor)
# Explicit local call only, with an owner-supplied executable definition:
result = runner.run(definition, client=exact_client, game_id=observed_game,
                    profile_id=verified_profile, owner_authorized=True,
                    cancel=threading.Event())
```

`observe(budget)` returns a `SkillObservation.from_facts(...)`. It must honor `budget.deadline` and `budget.cancel`, use current trusted GameLens observation records, and derive client/game/profile plus target/capture-session/geometry identity from the selected runtime. Facts must be independently observed outcomes, not action labels or supplied success flags. An HTTP body or saved profile is not trusted live provenance. The framework cannot attest a host callback's truth or preempt arbitrary observer code; the host must use bounded capture reads. `RunBudget.remaining` and `check()` support this contract.

`GuardedActionExecutor` accepts an existing GameLens instance implementing `submit_sequence`, `sequence_capacity`, and `safety.kill`. It uses the existing `arbiter.parse_sequence` before dispatch, binds the exact observation ID, fixes source to agent and rebind=False, and never arms, goes live, focuses, launches, retries, relaxes a threshold, or invokes a raw input backend. Callers cannot provide executable hooks or a raw-action callback through the definition. These Python interfaces are for trusted host code; they are not a security boundary against malicious code already running inside the process.

A single daemon callback thread submits one guarded decision; waiting is bounded to the parsed guarded sequence duration plus the GameLens wait and 250ms (at most 5.75 seconds under the existing five-second sequence ceiling). Cancellation, deadline, pending result, callback exception, or an unresponsive callback invokes the selected supervisor's existing `safety.kill`, which latches the session off and releases held input through existing cleanup. The adapter permanently closes afterward, and concurrent or later dispatches are denied even if the callback eventually settles. The framework never clears the emergency latch. Session recovery belongs to a subsequent explicit owner instruction. Cancellation cannot turn an already injected press into an unperformed action; receipts are retained where available and uncertain outcomes require inspection. The host's observer remains subject to its cooperative bounded-read contract.

A `sent` dispatch alone never completes a step. Verification requires a valid post-injection `after_frame` stamp, a strictly newer frame and different observation ID, unchanged client/game/profile and identity, and all expected facts. Missing stamps or pending execution are uncertain; refused/dry/partial dispatches fail; no automatic replay occurs. Later decisions follow only verified outcomes. Final skill outcomes are checked separately. Results include the exact definition hash, consumed observation IDs, dispatch receipts, completed step count, status and reason.

Reference-only catalog workflows remain reference-only even when authorized. Character-specific compatibility is not generalized from a launcher nickname or another player's calibration. The generic Marathon reference uses the existing godsarena-marathon profile; adapting Atong69 requires its verified character profile and fresh prerequisites. The synthetic fixture profile in the tests is not a live game profile.

Validation lives in `tests/test_game_skills.py`: immutable/versioned definitions, guard delegation, exact compatibility, fresh outcome provenance, failure/no-replay, cancellation and timeout, pending/stuck callback shutdown, and real InputExecutor cleanup with all native SendInput calls replaced by a recorder. No fixture launches or controls a real game. Reviewed learning is a separate advisory store; approved candidates do not enter the runner or alter guards automatically.
