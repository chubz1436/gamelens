# Remaining GameLens phases
Phase1 standalone read-only setup checker is integrated unchanged from c4f0861.
Phase2 adds passive bounded run correlation with UTC stamps and monotonic latency,
original and executed observation provenance, dashboard log/action IDs, explicit retry
attribution, truthful pending/terminal and objective distinctions, and video basenames.
Disk is off by default. CLI --metrics-directory BASE enables local checkpoints under
BASE/run-metrics. Operator POST /metrics/checkpoint or orderly shutdown persists;
no filesystem work runs in the input callback. Limits and refusal behavior are in RUN_METRICS.md.

Phase3 gamelens_see accepts crop=full/hud/minimap/dialog. Crop view tokens preserve
full-parent observations, capture session, geometry and freshness. /act maps shown-image
coordinates back into the parent's transport space before the existing guards and full-JPEG
rebind checks. Cropped anchors require a new full frame. Stale/uncertain crop requests fall
back full with metadata; fallback grants no input authority. Arbitrary normalized profile
regions are available through the local perception API. See CROPPED_PERCEPTION.md.

Phase4 versioned local skill definitions and the guarded runner check prerequisites,
profile compatibility, timeout/cancel and actual fresh outcome evidence. Existing Marathon,
open and close workflows are referenced without route duplication or remote execution.
GET /skills and gamelens_skills list metadata only. See GAME_SKILLS.md.

Phase5 /learning/proposals accepts evidence-derived advisory proposals with passing tests.
Agent tools can only read proposals. Operator credentials alone can review exact hashes and
mark approved candidates active. Activation is an inert reviewed advisory export; it never
changes runtime input, safety policy or automatic runner behavior. Rejected/unreviewed candidates
cannot activate. See REVIEWED_LEARNING.md.

Authenticated read-only MCP tools: gamelens_metrics, gamelens_skills, gamelens_learning.
Named clients require explicit client selectors for every tool; no broadcast or default.
Owner objective reports use operator POST /metrics/objective with explicit success and
observation/action/video reference identifiers. These reports do not infer game success.

Validation uses synthetic frames, fake HTTP/MCP boundaries and stubbed native inputs only.
No game acceptance, installation or deployment is implied. Project-local evidence is under
evidence/remaining-phases-20261004. The root and earlier integration owner work are preserved.
