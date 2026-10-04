# Reviewed-learning worker evidence

Attribution: /root/skills_framework/reviewed_learning. Requested model GPT-6.1 Sol; observed model not independently verifiable. No fallback or subagents used.

Authored only gamelens/reviewed_learning.py, tests/test_reviewed_learning.py, docs/REVIEWED_LEARNING.md, and this evidence fragment. Read-only references: game-control SKILL.md, docs/SAFETY_REVIEW_20261001.md, repository inventory and README/PLAN searches. No applicable AGENTS.md found in checkout/work/workspace searches.

Implemented bounded immutable JSON proposals, SHA256 binding failure evidence/candidate/test evidence, explicit authenticated-owner integration contract, exact-hash approval and separate activation, restart fail-closed, reject deactivation, no automatic activation, no code/input/safety-policy candidate fields. Persistence is local single-writer and failure leaves memory unchanged.

Test execution pending coordinated root process; worker did not start Python. No installs, live games, input, session controls, recording, publishing, deployments, or protected-branch edits. Obsidian task log pending outside writable scope. apply_patch encountered false sandbox path failure; authorized native PowerShell writes used require_escalated successfully.

Follow-up review hardening: entire proposal envelope validated and size-bounded before persistence/memory commit. Durable reload validates strict record/review/history schemas, JSON decoding raises LearningDenied, and compact writer/store envelope bounds align. Added regression cases for oversized envelope with empty unchanged store, malformed JSON/schema, and invalid history/review/state. Tests still pending root coordination.

Independent P1 review fix: random exclusive temporary creation replaces predictable open(w), rejects symlink and Windows reparse destinations/ancestors during load and writes, and cleans created temporary files on failure. Tests cover legacy temporary link preservation, linked destination on load/write, linked parent rejection, and exclusive-name collision without truncation. Link tests skip explicitly if symlink privileges unavailable. No Python test process run by worker.

PR6 bounded-history fix: repeated same-owner/same-hash already-active activation is idempotent; history retains latest100 transitions rather than denying review at capacity. Reject clears activation and persists truthful latest rejection even after history saturation. Added repeatedactivation/reject and100reviewtransition/activate/reject durable regressions preserving immutable proposal. API signatures unchanged. Root sole tester; no worker Python process.
