# GameLens — Implementation Plan (rev 5)

Low-latency Windows capture + AI control harness for a **native PC game window**.
Purpose-built for agent vision; it is not a video recorder.

> Rev 2 incorporated round-1 findings GL-001..GL-010. Rev 3 incorporated round-2 findings
> (GL-002/004/007/008 continued, GL-011 new). Rev 4 incorporates round-3 findings
> (GL-002 continued; GL-012, GL-013 new). Rev 5 incorporates round-4 finding GL-014.
> Changes marked `[GL-nnn]`.

## 1. Goal

Give an AI agent a continuously fresh view of one game window and a safe, accurate way to
click and type into it, with end-to-end capture->frame-available latency low enough that
model inference is the only meaningful bottleneck.

## 2. Observable acceptance criteria

| # | Criterion | How it is proven |
|---|---|---|
| A1 | Ambiguous target resolution **raises**; it never picks a match | `test_identity.py::test_ambiguous_refuses` |
| A2 | WGC is refused unless the set of windows owning the snapshot title is **exactly `{target_hwnd}`** and the target still wears that title — checked before bind, after first frame, and every frame after | `test_identity.py::test_title_reassigned_during_startup`, `::test_other_window_steals_title` |
| A3 | Sustained >=30 fps of **distinct native frames** (deduplicated by native timespan) | `bench_capture.py` reports distinct fps and duplicate count separately |
| A4 | A consumer holding a frame for 1s observes **no pixel mutation**; RSS flat over 60s | `test_frame_ownership.py` hashes a leased frame before/after 1s of capture |
| A5 | A capture call that **never returns** still fails over within the deadline, and its late result is discarded rather than published | `test_backend_health.py::test_never_returns`, `::test_returns_after_failover` |
| A6 | Frame pixel (x,y) round-trips to the correct screen pixel at 100/125/150% scaling **and** on a negative-origin secondary monitor | `probe_coords.py` + `probe_inject.py` reading back `GetCursorPos` |
| A7 | Injection is refused unless every interlock passes; each denies independently | `test_safety.py` |
| A8 | Safety fails **closed**: watchdog thread death denies the *next* action, not 500ms of them | `test_safety.py::test_watchdog_death_immediately_after_heartbeat` |
| A9 | Kill during a move->click dwell prevents the button-down, and any already-pressed button/key is released exactly once | `test_input_cancel.py` |
| A10 | An **unauthenticated** `GET /` discloses no usable credential; control endpoints reject a missing/foreign token, `Origin`, or `Host` | `test_http_auth.py::test_shell_leaks_no_credential` |
| A11 | The agent token cannot arm or go live, however it is presented | `test_http_auth.py::test_agent_token_cannot_arm` |
| A12 | An action is rejected when its **source frame** was stale or its backend session retired, even with fresh `created_at` and unchanged geometry | `test_arbiter.py::test_stalled_capture_unchanged_geometry` |
| A13 | Inference finishing after expiry or after a fast-tier preemption yields no action | `test_arbiter.py::test_inference_completes_after_preemption` |
| A14 | A mark label containing SVG markup renders as literal text and executes nothing | `test_dashboard.py` (DOM assertion) |
| A15 | Fast reflex tier reacts in <100ms p95 | `bench_reflex.py` |
| A16 | Dashboard shows live MJPEG + intent, and dry-run marks land on the intended button | manual visual check |
| A17 | An unchanged window is **not** reported stale, while a real move/resize is | `test_coords.py::test_matches_current` |
| A18 | A worker thread holding a lower DPI context is refused, even though the importing thread was per-monitor | `test_dpi.py::test_worker_thread_lower_context` |
| A19 | A kill cannot complete between an approval and the press it approved; a press committing concurrently with a kill is tracked **before** cleanup enumerates what to release | `test_safety.py::test_commit_and_kill_are_totally_ordered` |

## 3. Approach

### 3.1 Capture (`capture.py`)

**Target resolution `[GL-002]`.** `windows.find_window` now **raises `AmbiguousTarget`**
rather than choosing the largest match. Picking one would be an arbitrary choice made
*before* any later identity check runs, and no downstream check can repair it — the entire
safety story rests on the captured window and the injected window being the same one.
Callers with two same-titled games must pass an explicit HWND. *(Implemented.)*

**WGC identity is evidence, not proof `[GL-002]`.** Stated plainly: `windows-capture` 1.4.2
binds by `window_name` and **exposes no way to read back the HWND it actually bound**
(verified at `__init__.py:163-198`). We therefore cannot prove which window WGC captured.
What we can do is make a wrong binding detectable and refuse to run through it:

1. Resolve the HWND ourselves (unambiguously, per above) and snapshot its title.
2. **Ownership, not uniqueness `[GL-002]`.** Require `title_owners(snapshot_title) ==
   {target_hwnd}` *and* `GetWindowText(target_hwnd) == snapshot_title`. A uniqueness test
   alone passes the exact attack it is meant to catch: if the target renames itself from
   "Game" to "Other" while an unrelated same-sized window takes "Game", the title is still
   unique, the target is still alive and the dimensions still match — but the title now
   belongs to the *other* window, which is the one a title-binding backend attaches to.
   *(Implemented as `windows.title_still_owned_by`.)*
3. Apply that same ownership check **immediately before** `start()`, again **after the first
   frame** together with a dimension match, and **every frame** thereafter.
4. Any failure stops capture and fails closed; it does not degrade silently.
5. `--backend printwindow` is the **HWND-exact** path, available to an operator who needs
   identity proof rather than evidence. It is selected automatically whenever the WGC
   preconditions cannot be met.

**Residual gap, stated plainly.** Ownership can still change in the interval between the
last check and the native bind. The checks narrow that window; they do not close it, because
1.4.2 offers no bound-HWND readback to close it with. This is the whole of the "evidence,
not proof" limitation, and PrintWindow is the way out of it.

**Frame delivery.**

- **Duplicate suppression `[GL-009]`.** In 1.4.2 the padded-row branch calls
  `frame_handler` **twice for the same frame** (`__init__.py:252` and `254-257`), which
  occurs whenever `row_pitch != width*4` — common for alignment-padded D3D11 staging
  textures. We deduplicate on the native `timespan` before publishing and count distinct
  vs duplicate frames separately. Runtime dedupe rather than a version pin, so the fix does
  not depend on upstream.
- **Pixel ownership `[GL-003]`.** A bounded **buffer pool** (default 4). The callback leases
  a free buffer, copies into it, and publishes under a short lock. Consumers hold a
  refcounted lease; a buffer cannot be recycled while leased. Pool exhaustion drops the
  frame and counts it. Buffers are reallocated on resize.

**Stalled-backend recovery `[GL-008]`.** `PrintWindow` is synchronous and serviced by the
target application: if the game hangs, the call may never return, and a deadline the worker
checks *itself* can never fire. Therefore:

- Blocking backends run in a **terminable helper process**, not a thread. A hung
  `PrintWindow` in a thread cannot be interrupted; a process can be killed.
- An **independent supervisor** owns the deadline and **never joins** a blocked worker on
  the transition path. It terminates the helper and moves on.
- Every backend activation gets a **session generation id**. Publications carry their
  session id and are **rejected if the session has been retired**, so a `PrintWindow` result
  that returns *after* failover cannot overwrite frames from its replacement.
- Fallback order `wgc -> printwindow -> mss`, entered automatically. `GAMELENS_BACKEND` is
  test-only; A5 is proven without it.

### 3.2 Coordinates (`coords.py`)

- `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` once, before any other Win32 call.
- **Verify, do not assume `[GL-010]`.** The effective context is read back via
  `GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext())`. If it is not
  per-monitor, mapping and injection are **disabled** with an explicit reason rather than
  labelling virtualized rects as physical.
- **Per-thread, never cached `[GL-013]`.** DPI awareness is a *thread* context, so the
  process-wide `SetProcess...` attempt may be cached but its measured result may not.
  `require_trustworthy_dpi()` measures the **calling** thread on every call; caching the
  importing thread's value would let a worker running under a lower awareness sail past the
  guard and read virtualized geometry. *(Implemented; verified by forcing a worker thread to
  `DPI_AWARENESS_CONTEXT_UNAWARE` — it is refused while the main thread still passes.)*
- `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` for the visible rect, not
  `GetWindowRect` (~7px invisible resize border on Win10). *(Implemented.)*
- Every geometry read carries a **generation counter**, bumped on move/resize/DPI change.
- **Staleness compares placement only `[GL-012]`.** `Geometry.matches_current()` must not use
  dataclass equality: `captured_at` is a fresh `time.monotonic()` on every snapshot, so an
  unchanged window would always compare unequal and *every* action validated through it would
  be rejected as stale. *(Implemented: compares the placement fields via `_same_placement`;
  verified that an unchanged window now reports current.)*

### 3.3 Input (`input.py`)

- `SendInput` via ctypes. **`ULONG_PTR` must be `c_uint64` on 64-bit** — `wintypes` has no
  ULONG_PTR and DWORD yields a short struct SendInput rejects silently. Assert
  `sizeof(INPUT) == 40` at import.
- **Absolute normalization `[GL-001]`** subtracts the virtual-desktop origin:

  ```
  nx = round((x - SM_XVIRTUALSCREEN) * 65535 / (SM_CXVIRTUALSCREEN - 1))
  ny = round((y - SM_YVIRTUALSCREEN) * 65535 / (SM_CYVIRTUALSCREEN - 1))
  ```

  Coordinates outside the virtual desktop are **rejected**, not clamped into a wrong monitor.
- `KEYEVENTF_SCANCODE` via `MapVirtualKey`, with `KEYEVENTF_EXTENDEDKEY` for arrows/nav.
- **Check the return count.** A short `SendInput` return is an error (commonly UIPI) and is
  surfaced, not ignored.
- **Interruptible serialized executor `[GL-005]`.** One executor thread; safety rechecked
  immediately before **each** press, not once at authorization. Every press is tracked; on
  stop/kill the executor cancels pending work and emits **bounded cleanup releases** for
  exactly the tracked presses, never a new press. Otherwise "deny everything after kill"
  leaves keys physically held in the game.

### 3.4 Safety (`safety.py`)

Disarmed by default. `guard()` allows only when every condition is affirmatively true; any
exception inside it denies.

1. `armed` — set explicitly by the operator, never by model output
2. `not killed` — latched; only a process restart clears it
3. `GetForegroundWindow() == target_hwnd`
4. token-bucket rate limit (default 10/s)
5. **watchdog liveness AND freshness `[GL-006]`** — `thread.is_alive()` *and* heartbeat
   younger than 500ms. Heartbeat initializes **invalid**; thread failure latches disarm in a
   `finally`. The 500ms window is the detection bound for a *hung but alive* thread only.

**No gap between approval and press `[GL-014]`.** `check()` must release `_lock` to poll the
foreground window and the rate limiter, and a kill completing inside that gap would be
followed by an approved press — *after* cleanup had already run, so nothing would ever
release it. A separate **dispatch boundary** (`_dispatch`, an RLock) therefore covers both
sides: `commit()` evaluates every interlock and runs the press body while still holding it,
and `kill()` holds it across the latch and its cleanup callbacks. The ordering is total —
either the press commits first and is tracked before cleanup enumerates, or the latch lands
first and the press is refused. Lock order is always `_dispatch -> _lock`. Slow work (dwells,
model calls) stays outside the boundary. `require()` remains for advisory read-only checks
and is documented as not sufficient for a press. *(Implemented; `test_safety.py` pauses a
check mid-flight, races a kill against it, and asserts `["press", "cleanup"]`.)*

Kill switch: daemon thread polling `GetAsyncKeyState(VK_F12)` / `VK_PAUSE` every 20ms.
`dry_run=True` by default.

### 3.5 Server (`server.py`)

**Loopback is not authorization `[GL-007]`**, and neither is a token the server hands out
for free. Embedding the token in the page served by `GET /` would let any local process GET
`/` and read it back, which makes the token worthless. Therefore:

- `GET /` serves an **unauthenticated shell containing no credential**. The operator pastes
  the token printed once to the GameLens console; the page keeps it in `sessionStorage` for
  that tab. *(Implemented in the dashboard.)*
- **Two distinct capabilities, two tokens.** The **operator token** may arm, go live and
  stop. The **agent token** may only submit actions to `/act`. An agent credential can never
  arm the system, however it is presented — model output must not be able to grant itself
  authority.
- `Origin` must be absent or our own; `Host` must match an allowlist (blocks DNS rebinding
  into the control path).
- `/stream.mjpg` accepts the token as a query parameter because an `<img>` cannot set a
  header; it is equivalent to the header and never leaves the loopback origin.
- Endpoints: `GET /`, `/stream.mjpg`, `/frame.jpg`, `/windows`, `/state`;
  `POST /act` (agent or operator), `POST /arm`, `/live`, `/stop` (operator only).

### 3.6 Action arbiter (`arbiter.py`)

Both agent tiers and `/act` route through **one** arbiter. Provenance is **snapshotted
before inference begins and propagated unchanged `[GL-004]`** — stamping it when inference
*finishes* would reset the TTL and hide a preemption that happened during inference:

`target_hwnd`, `backend_session_id`, `frame_id`, `frame_captured_at`, `frame_w/h`, the
preprocessing transform (downscale + crop used for the vision image), `geometry_generation`,
`preemption_counter`, `created_at`.

An action is rejected when the target differs, the **backend session has been retired**, the
**source frame is older than the observation deadline**, the geometry generation moved, the
action exceeded its TTL (800ms), or the preemption counter advanced since the snapshot.

The source-frame check is what catches the case geometry checks miss: with the game still
foreground and the window unmoved, a **stalled capture** leaves an old frame in place, so an
action built from it has a fresh `created_at` and unchanged geometry and would otherwise
pass every rule while describing a scene that is seconds out of date.

HTTP callers reference a frame **by id only**; the server resolves its metadata from
server-held state and never trusts caller-supplied timestamps or generations.

### 3.7 Agent (`agent.py`)

- **Fast tier** (every distinct frame): numpy pixel probes + `cv2.matchTemplate` on ROIs.
  Handles sub-100ms reactions; bumps the preemption counter when it acts.
- **Slow tier** (2-5 fps): downscaled JPEG -> Claude vision -> actions carrying the
  provenance snapshot taken *before* the call.

### 3.8 Dashboard (`static/dashboard.html`)

Marks carry model-authored text. **Never build SVG by string concatenation `[GL-011]`**: a
label such as `</text><image href=x onerror=...>` becomes executable same-origin markup, and
this page can reach the operator endpoints. Elements are created with `createElementNS` and
label text assigned via `textContent`; coordinates and ages are validated as finite numbers
before becoming attributes. Keeping model strings out of executable markup is enforced here
regardless of any upstream validation. *(Implemented.)*

## 4. Trade-offs and non-goals

- **Non-goal:** encoding/recording to disk or RTMP. That is OBS's job; an encoder only adds
  latency here.
- **Non-goal:** anti-cheat evasion, kernel drivers, hiding injection. `SendInput` is a
  documented user-mode API; games using RawInput with `RIDEV_NOLEGACY` may legitimately
  ignore it. Documented, not worked around. Whether automating a given game is permitted is
  the operator's call.
- **Limitation:** UIPI blocks injection into elevated windows unless GameLens is elevated;
  surfaced by the SendInput short-count check.
- **Limitation:** WGC target identity is evidence, not proof, because the pinned dependency
  exposes no bound-HWND readback. PrintWindow is the HWND-exact alternative.
- **Trade-off:** the buffer pool drops frames under load by design. Correct for control,
  wrong for recording — see non-goals.

## 5. Assumptions

| Assumption | Source | Status |
|---|---|---|
| Windows 10 Pro 19045, Python 3.11.2, RTX 4060 | probed | verified |
| deps installed in `.venv` | pip + import check | verified |
| `windows-capture` 1.4.2 has no HWND selector or readback | read `__init__.py:163-198` | verified |
| `windows-capture` 1.4.2 double-fires padded frames | read `__init__.py:236-257` | verified |
| effective DPI awareness is per-monitor | probed at runtime: enum 2 | verified |
| target game runs non-elevated | not verified | fails closed via short-count error |
| `ANTHROPIC_API_KEY` present | not verified | slow tier disabled without it; fast tier runs |

## 6. Verification

```
.venv/Scripts/python.exe -m pytest tests/ -v
.venv/Scripts/python.exe tools/bench_capture.py --seconds 10
.venv/Scripts/python.exe tools/probe_coords.py
.venv/Scripts/python.exe tools/probe_inject.py     # reads back GetCursorPos
```

**Implemented and passing so far:** `dpi.py`, `windows.py`, `coords.py`, `safety.py`,
`input.py`, `static/dashboard.html`; `tests/test_safety.py` (14 tests). Still unwritten:
`capture.py`, `arbiter.py`, `server.py`, `agent.py` and their tests.

**Honest limit:** A6, A9 and A16 require a real game window and an operator at the machine.
Automated tests cover the transforms, the interlocks, the arbiter and the dashboard sink;
they cannot establish that a button was actually hit.
