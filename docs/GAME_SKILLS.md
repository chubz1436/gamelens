# Reusable game skills

`gamelens.game_skills` provides an inert workflow catalog and a local trusted-host executor for owner-supplied bounded skill definitions. Importing, registering or listing definitions sends no input, launches no game and changes no session. There is no HTTP/MCP execution endpoint.

The three built-in entries (GodsArena Marathon, opening clients, closing clients) are **reference-only metadata**. They reuse the existing Marathon plugin/profile and symbolic `installed-skill:godsarena-open-clients` / `installed-skill:godsarena-close-clients` references. They contain no executable decisions, copied routes or launcher scripts. Catalog listing does not imply those built-in workflows are executable through the framework; executing their established procedures remains a separate explicitly requested workflow.

`SkillCatalog.reference_catalog().list_skills()` returns detached JSON. `get(skill_id, version)` selects an exact semantic version, and duplicate ID/version registration is refused. `SkillDefinition.from_dict()` validates schema_version=1, source references, explicit game/profile compatibility, prerequisites, expected outcomes and finite bounds. At most 32 steps and 64 observations are allowed, with timeout at most 3600 seconds. Each step requires both prerequisites and outcome checks. Checks compare present scalar facts with exact type; missing or truthy-but-mismatched values fail. `definition_hash` is SHA256 over the canonical full immutable definition.

Executable definitions require `profile_pins`, one exact `ProfilePin(profile_id, version, sha256)` for every compatible profile ID. A profile ID alone grants no compatibility. Pins use explicit version strings and lowercase SHA256 digests. `profile_content_hash(profile_data)` hashes the exact canonical JSON snapshot that the host reviewed. Pin changes alter the definition hash. Older executable definitions lacking pins fail closed; reference-only definitions may leave pins unbound because they cannot dispatch. Character compatibility must be independently verified rather than inherited from another player's profile or a launcher nickname.

The ready-to-use local adapter is `GameLensSkillHost`:

```python
from gamelens.game_skills import GameLensSkillHost, ProfilePin, profile_content_hash

pin = ProfilePin(profile_id, reviewed_version, profile_content_hash(reviewed_profile))
host = GameLensSkillHost(
    existing_gamelens, client=exact_client, game_id=verified_game,
    profile=pin, perceive=perceive_facts, profile_provider=current_profile_pin,
)
result = host.run(user_defined_skill, owner_authorized=True, cancel=cancel_event)
```

The caller must supply the selected real GameLens runtime and a reviewed client/game/profile binding. Optional `profile_provider()` returns the current loaded `ProfilePin`; changes to its ID, version or content hash are refused before observation and again after perception. Omit it only when the host owns a fixed immutable profile snapshot. It must be supplied when the host supports switching or editing profiles. A declared pin does not attest an external file that another component can mutate; compute current pins from actual loaded content in that case. There is no automatic profile adaptation or learned-candidate activation.

The injected `perceive_facts(jpeg_bytes, original_observation)` is a pure trusted perception function returning a JSON facts object. The adapter calls the real runtime's nonblocking `encode_frame(retain=True, crop=None)` and resolves its token through the original `ObservationRegistry`. It passes the original JPEG and immutable Observation to perception, then rechecks registry retention and the original arbiter's target, capture-session, geometry, preemption and freshness verdict before using the facts. Cropped/view tokens or missing original provenance are refused. Empty capture reads poll cooperatively within the cancellation/deadline budget; no capture/session worker starts.

`SkillObservation` requires an explicit original `captured_at`: it has no timestamp default. The host copies `Observation.frame_captured_at` and frame ID unchanged. Identity includes original target HWND, capture session, geometry generation, frame dimensions and preemption counter. The runner verifies exact client/game/profile ID/version/hash on admission and every subsequent observation, rejecting a same-ID changed profile. It also rejects future timestamps and capture ages above one second. Capture age deliberately uses the real process `time.monotonic()` domain shared by GameLens capture; an injected runner clock affects execution budgets only. No fact callback can refresh a stale capture by omitting its timestamp.

Lower-level integration remains available through `SkillRunner(observe, GuardedActionExecutor(existing_gamelens))`. The observer must preserve original provenance, use bounded reads and honor `RunBudget.deadline`, `cancel`, `remaining` and `check()`. The framework cannot attest callback truth or preempt arbitrary pure perception/observer code. These are trusted-process interfaces rather than a security boundary against code already executing inside the process.

`GuardedActionExecutor` uses existing `arbiter.parse_sequence` and `GameLens.submit_sequence`. It binds the exact observation ID, source=agent and rebind=False, preserving the original registry, arbiter, watchdog, focus and per-press guards. It never arms, goes live, focuses, launches, relaxes thresholds or invokes raw input. Definition data cannot contain executable hooks. A single daemon callback thread waits for the parsed guarded sequence duration plus at most 0.5 seconds and a 0.25-second margin. Cancellation, deadline, pending result, callback exception or an unresponsive callback invokes the selected supervisor's existing `safety.kill`, releasing held inputs through existing cleanup and permanently closing the adapter. It never resets an emergency latch. Bounded callback quiescence is required before the next verified decision.

`sent` is never gameplay success. Each step requires a valid post-injection `after_frame`, a strictly newer full-frame observation with a different token, unchanged provenance and profile pin, and all expected facts. Pending or missing injection stamps are uncertain; dry/refused/partial actions fail. No automatic replay occurs. Final skill outcome checks are separate from step completion. Results retain exact definition hash, profile pins, observation tokens, receipts, completed count and status. Cancelled or uncertain input requires inspection; session recovery belongs to a later explicit owner instruction.

`tests/test_game_skills.py` covers pins at admission and midrun, unpinned legacy denial, timestamp omission/staleness, portable references, actual full-frame host registry/arbiter/submit integration, fresh outcomes, long and consecutive decisions, and real supervisor/InputExecutor cancellation cleanup with native SendInput fully replaced. No fixture controls a real game. Reviewed learning remains a separate advisory store whose activation cannot alter this runner or its safety policy automatically.
