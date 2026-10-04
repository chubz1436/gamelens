# GameLens run metrics and local retention

Run metrics are passive evidence. They do not arm a session, call input, capture images, start a recording, evaluate a game objective, or replay an action. The runtime uses in-memory metrics on its existing guarded paths. The HTTP metrics read is read-only; operator checkpoints write only when persistence was explicitly enabled at startup. Storage work belongs outside input callbacks and after input release during shutdown.

## Evidence model

`RunMetrics(max_runs=32, max_events=512, max_age_seconds=86400)` retains at most 32 runs and 512 events per run. Idle runs expire opportunistically on reads and writes. Event eviction preserves cumulative counts and `dropped_events`; correlation latency becomes unknown when the corresponding action record has been evicted. Snapshots are detached copies and event sequence allocation is protected by a lock.

Every event carries a UTC ISO timestamp and monotonic elapsed milliseconds. Action outcome latency uses the same process monotonic clock as action recording; wall-clock corrections cannot alter it. Source frame age is recorded from the observation's original `frame_captured_at`, with optional `freshness_limit` in seconds. Future source timestamps retain a signed negative frame age and report fresh=false; they are never made fresh by clamping. This evidence never changes the arbiter's existing freshness rules.

| API | Meaning |
| --- | --- |
| `begin_run()` | Create a server-generated run ID; optional bounded explicit ID for offline callers. |
| `record_observation(run_id, observation_id, observation, source='snapshot', freshness_limit=None)` | Copy numeric provenance, frame age and optional known freshness; exclude image buffers and arbitrary fields. |
| `record_action(run_id, action_id, observation_id=None, kind='action', attempt=1, retry_of=None, log_id=None, bound_observation_id=None)` | Correlate server action ID, original observation and optional dashboard log entry ID and separate executed/bound observation ID after rebind. IDs normalize to strings. |
| `record_outcome(run_id, action_id, outcome, verdict=None, ...)` | Record accepted/pending/completed execution separately from caller-reported objective success. Executor step counts, partial injection, churn and after-frame evidence are optional. |
| `record_objective(run_id, objective_id, success, evidence=())` | Explicit caller-reported boolean result with up to 32 copied observation/action/video IDs. No success is inferred from input or screen churn. |
| `link_video(run_id, video_id, reference, start_seconds=None, end_seconds=None)` | Retain a video filename and optional span. Directory names are stripped; no video is opened or modified. |
| `finish_run(run_id, status='completed', objective_success=None)` | Set terminal status exactly once. Completed runs may have false or unknown objective success. |
| `snapshot(run_id=None)` / `runs()` | Detached records; all-run snapshot also includes bounds and recording error count. |
| `safe_call(method, *args, **kwargs)` | Best effort boundary: errors are counted without escaping into input control. |

Outcomes are `sent`, `dry`, `denied`, `cancelled`, `error`, `pending` or `rejected`. Pending is nonterminal; an eventual terminal callback appends its own result. A late pending event cannot replace an already recorded terminal outcome. Terminal run statuses are `completed`, `failed`, `cancelled` and `aborted`. These describe execution or run lifecycle and do not prove success in the game.

Retries are evidence only: attempt 1 has no prior reference, and attempt >1 must name a distinct prior action ID. Neither metrics nor storage schedules an action. Callers continue to use the existing guarded dispatch and explicitly make each retry decision.

## Opt-in disk storage

`LocalRunStore(base_directory=None, enabled=False)` is disabled by default and construction creates no directory. Disabled saves return `{'saved': False, 'reason': 'disabled'}` and reads do not touch disk. The runtime enables storage only with its explicit startup directory option. `save(metrics.snapshot(run_id))` returns a fixed status dictionary; filesystem errors cannot alter input authority or escape into a dispatch caller. `snapshots()` reads valid retained snapshots and `status()` reports configuration and the last save result.

Storage manages only the fixed `run-metrics` child beneath the configured base directory. Run IDs become SHA-256 filenames; supplied IDs cannot select an arbitrary filename or cleanup path. Defaults are 512 events per snapshot, 32 committed directory entries, 8 MiB aggregate bytes, 256 KiB per file and seven days maximum file age. Writes use an exclusive temporary file, flush/fsync and atomic replacement. During replacement one additional temporary file of at most the file-size limit may exist; committed content remains within the quotas.

Retention is opportunistic on save. Reads filter expired files without deleting them. Cleanup is nonrecursive and checks immediate-child ownership and schema before every deletion. Only valid GameLens metadata envelopes whose filename matches their run ID are eligible for deletion. Foreign, malformed, oversized and crash-left temporary files count toward quotas and are never automatically removed. If they occupy the quota, saves fail with `storage_unavailable`; the owner must review them. Scans stop after 4096 directory entries. Saves are serialized within one store instance; configure a separate base directory for each process. Cross-process directory locking is not provided.

Symlinks and Windows junction/reparse points in the managed directory chain are rejected. Use an operator-controlled local directory. The module does not offer a recursive delete, arbitrary path cleanup, recording deletion, HTTP directory selector or credential-bearing export. Metadata schema validation rejects arbitrary request dictionaries, unknown nested fields, image bytes, nonfinite values and raw exception messages. References are bounded IDs and local video basenames; callers must never use credentials as IDs.

An optional `RunMetrics(sink=store)` API is available for offline callers; it serializes saves and catches sink failures. The game runtime keeps this sink unset because synchronous disk writes must not delay injection callbacks. Explicit operator checkpoints and shutdown saves provide the runtime persistence boundary.

## Verification

`tests/test_run_metrics.py` covers provenance copies, freshness, URL-safe IDs, monotonic latency, pending/terminal separation, explicit objective evidence, retries, count/event/age bounds, concurrent sequence allocation, video references and sink failure isolation. `tests/test_run_store.py` covers default-off behavior, atomic round trips, schema/media exclusion, file/event/byte/age quotas, preservation of foreign data, failure cleanup, existing snapshot preservation, link rejection when supported and concurrent bounded saves.

These tests use fake observations and temporary directories. They do not send live game input, change session authority or open media.

An idle runtime run that expires rotates to a new run ID on the next evidence operation.
Original timestamps are not refreshed; bounded retention still applies to expired runs.

Published playable interrupted clips remain correlated when the recorder finishes with a
file and positive frame count. Video evidence includes optional `interrupted: true`, never
the arbitrary interruption text or path. Active clips, missing published files and failed
finalization remain excluded. Older video events without this flag remain valid.
