# Reviewed learning

`ReviewedLearningStore` provides bounded, failure-derived advisory proposals. It does not execute skills, input, code, or safety-policy changes. Only description, observation_hint, and verification_hint text fields are accepted. Consumers must treat text as advisory data, never executable instructions or authority to bypass safety controls.

`propose(skill_id, failure_evidence, candidate, test_evidence)` requires outcome=failed plus nonempty evidence, and passed=true plus nonempty test evidence. Evidence is supplied by the caller; this framework does not independently attest its truth. Use actual immutable receipts and test results. Every proposal is a detached JSON snapshot. Its id/candidate_hash is SHA256 over all four proposal fields, including evidence and tests.

`list_proposals()` and `get(id)` are read-only snapshots. `review(id, candidate_hash, decision, owner, notes='')` accepts approve/reject; review clears activation. `activate(id, candidate_hash, owner)` requires the same owner's approval of the exact hash. New evidence or text creates a new hash requiring new review. There is no automatic activation path. `active_candidates(skill_id=None)` lists approved active advisory records.

HTTP/MCP adapters must derive owner from an authenticated operator channel and must not expose review/activation to agent credentials. A supplied owner string alone is not authentication. Read-only agent listing is permissible. The module never arms a session or alters any existing supervisor policy.

Optional path persistence uses fsync and atomic replace. Proposal count is bounded (default 100, max 1000), each payload/record is bounded to 256 KiB, and review history retains the latest 100 transitions. Full stores refuse additions rather than discard evidence. Persistence failure leaves memory unchanged. Use one store instance per durable path; cross-process shared writers are unsupported. Protect the durable file with filesystem access controls. Reload validates proposal hashes and resets approval/activation, retaining historical reviews; restarting cannot silently activate learned behavior.

Tests: tests/test_reviewed_learning.py covers approval/hash/owner binding, rejection, immutable evidence, unsafe candidate fields, required evidence, capacity, reload/tamper detection, and persistence failure.

Durable path hardening: destinations and all parent ancestors are rejected when symlinks or Windows reparse points. Temporary files use random names and exclusive creation (O_EXCL, plus O_NOFOLLOW where available), are fsynced, and are removed on failure. Existing predictable .tmp files are never opened. Storage must reside in a trusted directory without concurrent hostile directory-entry replacement; path checks do not provide cross-process isolation against ancestor swaps. Symlink tests explicitly skip where platform privileges prohibit creation; an exclusive collision test requires no links.

The default GameLens runtime constructs an in-memory store. Durable storage is optional
through the local library path argument; default HTTP proposals do not persist to disk.
HTTP proposal/review/advisory activation and objective mutations run off the ASGI event
loop with one outstanding evidence mutation per app. Contending requests receive429.
Cancelled requests retain admission until the actual worker finishes. /stop remains
independent and responsive during a stalled store operation.

Repeated activation by the already-approved owner for the same active hash is idempotent and does not append history. A new review, including rejection, retains the latest 100 transitions so exhausted history cannot block deactivation. Immutable proposal and evidence snapshots are never truncated. Persistence failures still leave memory unchanged and must be surfaced to the operator.
Learning and metrics evidence reads/checkpoints share bounded asynchronous admission;
readers cannot fill the control-worker pool while a durable writer holds its lock.

Byte capacity: proposals reserve enough metadata space for a rejection by a128-character owner with an empty note. Transition retention also drops oldest history until the record fits256KiB; count100 is an upper bound. If a rejection still cannot fit because of its optional notes, notes are omitted after history is exhausted. The current reject decision, exact hash, owner, and inactive state persist; immutable proposal/evidence are never truncated. Approval with oversized metadata remains refused. Read the returned review to see retained notes/history.

Owner identifiers are bounded both to128 characters and128 JSON-escaped content bytes (130 bytes including quotes). This makes rejection headroom exact even for Unicode/control-character identities. Valid shorter Unicode identities are supported; over-budget identities are refused before state mutation. Unicode notes use the same byte-aware history pruning/optional rejection-note omission. Reload rejects legacy records whose immutable proposal leaves insufficient rejection metadata headroom rather than accepting unreviewable state.

If full rejection history metadata cannot fit, a compact event=reject, decision=reject audit marker is retained; its owner and exact hash remain in the current review. Admission reserves this marker too. The latest rejection remains auditable even when optional notes and older history must be omitted.
