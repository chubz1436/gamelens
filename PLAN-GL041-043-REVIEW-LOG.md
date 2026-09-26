# GL-041 / GL-042 / GL-043 review log (2026-09-25)

- Builder: Claude (Claude Code), on the Hyper-V test VM (GameLens-HV, Bedrock 26.51 trial).
- Inspector: fresh Codex sessions, gpt-6-astra, effort high, `codex exec -s read-only`, private
  CODEX_HOME (auth + config + sandbox state copied from `~/.codex`). CLI 0.156.1; reads worked.
- No plan review: these were defects found and measured live, fixed with tests and mutation
  checks, then inspected. Also covered: the GL-040 round-2 fixes (RV01, RV02) that had never been
  inspected. Limits: 2 inspection rounds.

## Inspection 1 — REVISE (on 92d9311, a2d3eb1, f37fd2b, f44f869, 2a74630)

Seven findings, all accepted. RV01 and the MCP description change: no findings.

- **GL042-I01 (high)** A transition inside the activity window counted as motion, masking the
  content a second swap replaces. → moving = moved in 3 of 4 half-second buckets.
- **GL042-I02 (high)** Activity snapshot had no frame/session/size provenance. → snapshot up to
  the handed-out frame id, same session; history resets on session or native size change.
- **GL041-I01** A start outlasting `stop()`'s join installed an unsupervised backend. → commit
  under the lock, retire + reap a late start.
- **GL041-I02** A retired backend's frame could be served. → `encode_frame` checks the backend.
- **GL041-I03** Hung teardowns accumulated with retries. → at most 3 pending; then no new start.
- **GL043-I01** Bedrock menus animate; judging E after 3 frames is too early. → per-edition
  after-frames (Bedrock 15) and a second frame must agree before "unsupported".
- **GL040-RV02-I01** A missing mid-interval control frame was dropped silently. → inconclusive.

Fixed in 4c20d69. 542 tests in the guest; 18/18 mutants killed.

## Inspection 2 — REVISE (on 4c20d69)

GL041-I01, GL041-I03, GL040-RV02-I01 confirmed closed. Five findings, all accepted:

- **GL042-RV02-I01 (high)** Motion in three old buckets masked a region that had just stopped. →
  the latest bucket must also show motion.
- **GL042-RV02-I02 (high)** Encoder could run ahead of the poller across a resize. → snapshot
  takes the native size; no map unless session and size match.
- **GL041-RV02-I01** Backend dropped during the encode. → re-check after encoding.
- **GL041-RV02-I02** (pre-existing) `stop()` called a native stop inline. → bounded 2 s wait on a
  tracked teardown.
- **GL043-RV02-I01** The confirming second frame skipped the postconditions. → `_postcheck()`.

Fixed in bc50d20. 548 tests in the guest; 5/5 mutants killed.

## Inspection 3 — REVISE (on bc50d20, 80d5fc2, 4ca6147, a05d0f0, 7f77420)

The Owner asked for everything to be finished (2026-09-26), so a third round was run past the
original budget of two. GL042-RV02-I02, GL041-RV02-I01/I02 and GL043-RV02-I01 confirmed closed;
DLL pinning and the job object's pywin32 usage had no findings. Five findings, all accepted:

- **RV03-I01 (high)** The activity window ended at the last sample; a sampling stall carried old
  motion onto a newer frame. → the window ends at the moment of asking.
- **RV03-I02 (high)** A failed windows-capture import was cached as "legacy, bind by title"; a later
  2.x would be asked by title, a substring match. → unknown is its own answer, uncached, WGC refuses.
- **RV03-I03 (medium)** An untied PrintWindow worker ran anyway, and one killed before assignment
  orphaned. → startup fails when the tie fails; the worker exits once its parent is gone.
- **RV03-I04 (high)** The hotbar search accepted two bright strokes. → it needs the top edge too.
- **RV03-I05 (medium)** A bright frame was quadratic in columns. → vectorised per gap; frames over
  1280 wide shrunk first; a white 4K frame refused in 0.3 s.

Fixed in b23fec4. 580 tests in the guest; all mutants killed (8 detector, 5 others; two
survivors were answered by simplifying: an equivalent `now=` argument dropped, a test sharpened).

## Status

Three rounds. The round-3 fixes have not been independently inspected. Known margin, measured
live: on Bedrock's Play screen a late click on an opaque button at +4 s scored global 1.67 over
the still tiles against a limit of 2.0 (records expire at 5 s).
