# Reviewed-learning worker evidence

Attribution: /root/skills_framework/reviewed_learning. Requested model GPT-6.1 Sol; observed model not independently verifiable. No fallback or subagents used.

Authored only gamelens/reviewed_learning.py, tests/test_reviewed_learning.py, docs/REVIEWED_LEARNING.md, and this evidence fragment. Read-only references: game-control SKILL.md, docs/SAFETY_REVIEW_20261001.md, repository inventory and README/PLAN searches. No applicable AGENTS.md found in checkout/work/workspace searches.

Implemented bounded immutable JSON proposals, SHA256 binding failure evidence/candidate/test evidence, explicit authenticated-owner integration contract, exact-hash approval and separate activation, restart fail-closed, reject deactivation, no automatic activation, no code/input/safety-policy candidate fields. Persistence is local single-writer and failure leaves memory unchanged.

Test execution pending coordinated root process; worker did not start Python. No installs, live games, input, session controls, recording, publishing, deployments, or protected-branch edits. Obsidian task log pending outside writable scope. apply_patch encountered false sandbox path failure; authorized native PowerShell writes used require_escalated successfully.

Follow-up review hardening: entire proposal envelope validated and size-bounded before persistence/memory commit. Durable reload validates strict record/review/history schemas, JSON decoding raises LearningDenied, and compact writer/store envelope bounds align. Added regression cases for oversized envelope with empty unchanged store, malformed JSON/schema, and invalid history/review/state. Tests still pending root coordination.

Independent P1 review fix: random exclusive temporary creation replaces predictable open(w), rejects symlink and Windows reparse destinations/ancestors during load and writes, and cleans created temporary files on failure. Tests cover legacy temporary link preservation, linked destination on load/write, linked parent rejection, and exclusive-name collision without truncation. Link tests skip explicitly if symlink privileges unavailable. No Python test process run by worker.

PR6 bounded-history fix: repeated same-owner/same-hash already-active activation is idempotent; history retains latest100 transitions rather than denying review at capacity. Reject clears activation and persists truthful latest rejection even after history saturation. Added repeatedactivation/reject and100reviewtransition/activate/reject durable regressions preserving immutable proposal. API signatures unchanged. Root sole tester; no worker Python process.

Final byte-capacity review fix: _replace prunes history by serialized byte size as well as count; propose reserves empty-note rejection headroom for max128-character owner. If optional rejection notes cannot fit after dropping all history, notes are omitted, preserving current rejection/hash/owner/inactive state and immutable proposal evidence. Added exact261326-byte failure fixture plus durable reload/hash/evidence invariants and long128-character-owner/4096-note rejection regression. No Python process, commits, or pushes by worker.

Additional root reproduction coverage: HTTP-sized60079-character evidence with46long-note/34empty-note reviews thenactivation/rejection, preserving latest rejection/history and immutable hash. Unicode owner identity bound is128 JSON-escaped content bytes, Unicode notes receive byte-aware pruning/omission. Added nonBMP owner/note regression. Reload enforces rejection headroom on legacy proposal records. No worker Python process.

Root reported focused verification before final reserve-denial regressions:132passed,5symlinkskips,0failed. Worker added two initial-envelope-valid but rejection-reserve-insufficient admission/reload regressions; both assert no unsafe commit or legacy mutation. _checked now delegates to central owner validator. Owned-file EOF normalized. Root will rerun affected and aggregate suites; worker started no Python process and made no commits/pushes.

Integrator final validation checkpoint: sourcef39fc82ef70cb72cd82faac6319cb08337263204.
Focused134passed/5symlinkprivilegeskips/0failed8.23s; aggregate1108passed/5skipped/0failed121.91s,
one existing dependency warning. These replace prior pending/132-focused checkpoints.
Six new learning regressions included (34learningpassed,4linkfixtures skipped).
Independent source/coverage review has no unresolved finding. Worker started no Python process;
integrator owns final evidence publication and canonical vault resynchronization.
