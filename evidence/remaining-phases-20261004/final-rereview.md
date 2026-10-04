# Parent final-review remediation

Reviewed source: f39fc82ef70cb72cd82faac6319cb08337263204.
Supersedes the initial4d4302b handoff for learning byte capacity and interrupted video evidence.

## Reproduced byte-capacity failures in fe0239f source

Both examples ran against the immutable committed old module; raw evidence is in byte-boundary-reproduction.log.

| Example | Proposal | Before activation | Activated | Original result |
|---|---:|---:|---:|---|
| Library:261326-byte failure text, owner=owner | 261490B | 261980B | 262100B | Empty-note reject denied, still active |
| HTTP-sized:60079-byte failure text,46long-note and34empty approvals, owner=session-operator | 60243B | 261969B | 262100B | Empty-note reject denied below100events, still active |

The HTTP-sized body fits the existing64KiB boundary. Root also added a real authenticated HTTP lifecycle fixture, not merely a public-library reproduction.

## Corrected learning lifecycle

Mutable audit history now retains at most100events and prunes oldest events by canonical JSON byte capacity. The immutable proposal and its exact hash never change. Proposal admission and legacy loading reserve room for worst accepted owner identity, current exact-hash rejection state and a compact latest-rejection marker. A legal initial envelope lacking that reserve is refused without adding memory state or writing a proposal; a legacy record without reserve fails closed on load and its file remains unchanged.

At byte capacity, the latest rejection is retained. If the full event cannot fit, its compact reject marker remains; current review carries the exact hash and authenticated owner. If optional rejection notes alone prevent revocation after older history is exhausted, the returned/persisted notes are empty. Approval metadata that cannot fit remains refused. Callers must inspect returned notes/history. Owner identities are bounded to128characters and128JSON-escaped content bytes; valid short Unicode identities work, oversized escaped identities are refused before mutation. These explicit bounds make worst-case rejection reservation independent of Unicode escaping. Durable write failure remains an explicit failure and does not partially change memory.

Added library byte-boundary, maximum-owner/long-note, HTTP-sized multireview, non-BMP, admission reserve-denial and unchanged legacy-file reserve-denial tests. Reload fixtures verify inactive state and unchanged proposal/hash.

## Corrected interrupted clip evidence

Recorder already validates finalized MP4 containers and clears file on failed finalization. A playable interrupted recording can retain its published file and positive frame count alongside an interruption error. The shared stop/shutdown linker now accepts these finalized published clips, continues refusing active/missing/nonpositive/bool-frame snapshots, and preserves the warning as optional boolean interrupted=true. Arbitrary raw error text and paths do not enter metrics. Existing video events without this flag remain valid under the closed persisted schema.

Synthetic regressions cover normal and interrupted clips through HTTP stop and orderly shutdown, deduplication, persistence before terminal checkpoint, unpublished clips and legacy/new schema compatibility. Existing recorder and safety fixtures also pass in the focused suite. No actual recording or live input performed.

## Validation and disposition

Final affected suite:134passed,5Windows symlink privilege skips,0failures,1existing dependency warning,8.23seconds (final-rereview-focused-final.log/XML).
Complete aggregate:1108passed,5Windows symlink privilege skips,0failures,1existing warning,121.91seconds (final-rereview-aggregate.log/XML).
Unchanged Phase1 retains its prior92passed/3skipped/0failures result; no additional Phase1 source change or unsupported acceptance claim.
Independent source review: no unresolved substantive source or coverage finding at f39fc82; aggregate complete and final disposition closed.

All worker/subagent assignments remain explicit GPT-6.1 Sol; rollout metadata remains unavailable for independent identity verification. Root serialized every Python process. No native game input, settings, installations, merges or deployment. Parent CHUBot retains final overall PR6 review.
