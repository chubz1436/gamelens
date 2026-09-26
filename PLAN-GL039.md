# PLAN-GL039 — Agent controls: drive the game in fewer round trips, and stop borrowing the Owner's devices

Owner's request (2026-09-24): "gawan mo ng controls ang gamelens na mas mabilis mo or ng ibang
agent ma control ang laro na hindi mo na kailangan gamitin ang mouse at keyboard ko" — give
GameLens controls so Claude or another agent can control the game faster, without needing the
Owner's mouse and keyboard. Run through /claudex-route and /claudex-loop.

Authorization: build is authorized. **Commit/push is not** — the diff is left for sign-off.
Base for inspection: `ad8984a` (branch `claude/gamelens-agent-controls-e8a8db`, clean tree).

## What the request actually contains

Two separate problems, with different evidence behind them:

1. **Speed of agent control.** Today an agent drives GameLens with curl or the ad-hoc
   `play.py`/`bot.py` helpers: fetch `/frame.jpg` for an observation id, POST `/act` with one
   primitive, fetch `/frame.jpg?after=` to see the result. Three HTTP round trips and one agent
   tool call each, per primitive. Primitives cannot overlap: `key` is down-dwell-up in one
   sequence, so "walk forward while turning" or "sprint-jump" cannot be expressed at all, and
   anything longer than one primitive costs a model turn per step. There is no tool surface an
   agent (Claude Code, Codex, any MCP client) can load directly.
2. **Not using the Owner's devices.** `SendInput` is global: the cursor moved is the Owner's
   cursor, keys go to whatever is focused, and the `NOT_FOREGROUND` interlock means the game has
   to be the foreground window. The vault note *2026-09-22 - Can the harness stop stealing the
   keyboard* tried posted window messages and concluded the game (window class `SDL_app`)
   ignores input while unfocused. **That test was confounded**: `%APPDATA%\.minecraft\options.txt`
   has `pauseOnLostFocus:true` (read 2026-09-24), and `bot.hold_focus()` documents that
   singleplayer pauses on focus loss. Every probe case ran against a paused world, where E, W,
   clicks and mouse motion all do nothing *regardless of input path*. The conclusion is
   therefore unproven, not refuted — which is why part B starts with a probe.

## Goal and acceptance criteria

### Part A — agent control surface (buildable and provable offline)

- **A1. `kind: "sequence"` on `/act`.** One observation, one arbiter verdict, one executor
  `Sequence` built from a list of primitive steps that may overlap in time:
  `{"do":"key_down","key":"w"}`, `{"do":"key_up","key":"w"}`, `{"do":"button_down","button":"left"}`,
  `{"do":"button_up","button":"left"}`, `{"do":"look","dx":..,"dy":..}`, `{"do":"wait","ms":..}`,
  `{"do":"tap","key":"space","ms":..}` (sugar for down/wait/up).
  - Validated entirely at the HTTP edge / in the arbiter builder, **before** anything is queued:
    ≤ `MAX_SEQUENCE_STEPS = 64` steps; total dwell ≤ `MAX_SEQUENCE_SECONDS = 5.0` (same ceiling
    as `MAX_BUTTON_HOLD`); keys through a **narrower sequence allowlist** (review R1, below);
    buttons through
    `Button`; look deltas finite and clamped per step with `MAX_LOOK_DELTA`; `wait` finite and
    ≥ 0; `key_up`/`button_up` of something this sequence did not press → 400; a `key_down` of an
    already-down key in the same sequence → 400; unknown `do` → 400. Malformed input never
    reaches the executor (the "a raise after injection invites a double send" rule).
  - **No chords that Windows treats as shortcuts (review R1).** The single-key `key` action is
    safe with `KEY_NAMES` because it holds one key at a time; a sequence can overlap keys, so
    `alt`-down, `tab`-tap would be Alt+Tab. Sequences therefore accept only
    `SEQUENCE_KEYS` = letters, digits, `space`, `shift`, `ctrl`, and the four arrows — canonical
    VK codes, checked for every expanded key-down including `tap`. `alt`, `tab`, `escape`/`esc`,
    `enter` and the F-keys are refused inside a sequence with a 400 naming the single-key
    `key` action as the way to send them. With Alt, Tab, Escape and the Windows keys absent,
    the reachable chords are Ctrl/Shift + letter/digit/space/arrow, which Windows delivers to
    the foreground window (the game, under the unchanged `NOT_FOREGROUND` interlock at every
    press) rather than intercepting: Alt+Tab, Alt+F4, Ctrl+Esc, Ctrl+Shift+Esc and Win+… are
    all unreachable. Tests enumerate those and assert 400 before `submit`.
  - **Every input the sequence pressed is released at its end**: the builder appends the missing
    `KeyUp`/`ButtonUp` steps in the order pressed. A sequence cannot leave anything held after it
    completes. Kill/deny mid-sequence is already covered by `release_all` (unchanged).
  - New-input step count (key_down, button_down, look, tap-down) must be ≤ the rate bucket's
    capacity, else 400 naming `--rate` — a sequence the limiter cannot possibly admit is refused
    up front instead of being denied half-way through. A sequence can still be denied mid-way by
    a bucket drained by other traffic; that is today's semantics (denied + release_all) and is
    reported as `denied`.
  - **Freshness at press time.** `Arbiter.submit`'s `revalidate` currently re-runs `evaluate()`
    before every new input, including `STALE_OBSERVATION` (> 1.0 s) and `EXPIRED` (> 0.8 s).
    A 3-second sequence would be refused at its first `look` after 0.8 s. Resolution: an
    `Action` gains `committed_at_first_input: bool` (True only for sequences). For those, the
    freshness rules (`STALE_OBSERVATION`, both `EXPIRED` checks) apply at submission and at the
    **first** new input only; every later new input still re-checks `WRONG_TARGET`,
    `RETIRED_SESSION`, `GEOMETRY_MOVED` and `PREEMPTED`, and every safety interlock via
    `safety.commit()`. Justification: a sequence is one decision about one frame, exactly as a
    2-second `key` hold already is (its `KeyUp` is never freshness-checked today); the 5 s cap
    bounds how far it can run past the frame. Everything that means "the world is no longer the
    one reasoned about" still stops it.
  - Response is the existing `Dispatch` shape. `_expected_duration` already sums dwells, so the
    wait budget covers the sequence's length.
- **A2 (considered, rejected): an inline `return_frame` option on `/act`.** Act-then-see is done
  client-side in the MCP server (A3) with the existing `/frame.jpg?after=`, so the HTTP contract
  does not change.
- **A3. MCP server, `python -m gamelens.mcp`.** A stdio MCP server that is an HTTP client of a
  running GameLens, given **the agent token** (env `GAMELENS_AGENT_TOKEN`, or `--token-file
  PATH`). It never calls `/arm`, `/live`, `/stop` or `/windows`, and the README says to give it
  the agent token only; the server's role check is what actually enforces it (an agent token
  gets 403 on those routes).
  Hand-rolled JSON-RPC 2.0 over stdio (newline-delimited), no new dependency — the venv has no
  `mcp` package and installing one is not needed for five tools. Implements `initialize`,
  `notifications/initialized`, `ping`, `tools/list`, `tools/call`; unknown methods → JSON-RPC
  error -32601; logs to stderr only (stdout is the protocol).
  Tools:
  - `gamelens_state` → the `/state` JSON (armed, live, killed, fps, target), text.
  - `gamelens_see(quality=50)` → current frame as MCP `image` content (`image/jpeg`, base64) plus
    a text line with frame id, observation id, size. Remembers the observation id.
  - `gamelens_act(action, see_after=true, after_frames=3, quality=50)` where `action` is one
    `/act` body minus `observation_id` (click/key/press/look/sequence). Returns the `/act` JSON
    as text and, when `see_after` and `after_frame` is non-null, the
    `/frame.jpg?after=<after_frame>&frames=<n>` image in the same tool result; that frame's
    observation becomes the remembered ("shown") one. One tool call = act + see. A 409/504/503
    is a normal tool result with `isError: true` and the server's detail, never a protocol error.
  - **Which observation an action is bound to (review R1, R2, R3).** The arbiter refuses an
    observation older than 0.8 s (`ACTION_TTL`), and a model's own turn takes 0.3–2 s, so an
    MCP action will usually find the shown observation expired. Rebinding is therefore needed,
    and it is done **on the server**, where the shown observation's provenance lives — never by
    a client fetching a new id and resubmitting.
    - The MCP server remembers the **shown** observation id: the one attached to the last image
      actually returned to the agent. No shown observation → nothing is dispatched; the tool
      returns a fresh image and says to decide on it.
    - `/act` gains `"rebind": true` (default false; HTTP callers are unaffected). With it the
      server:
      1. Resolves the shown observation from the registry. **Not on record (registry TTL 5 s
         or evicted) → deny `STALE_OBSERVATION`, fail closed**, no fresh binding.
      2. Takes a fresh observation from the newest frame, with the shown one's transport
         `scale` and `crop`.
      3. `Arbiter.rebind_allowed(shown, fresh)` requires, all of: same `target_hwnd`, same
         `backend_session_id` and that backend not retired, same `geometry_generation`, same
         `preemption_counter`, same frame width/height. Any mismatch → deny with that rule's
         own `Rejection` (`PREEMPTED`, `GEOMETRY_MOVED`, …). So a reflex that acted in between,
         a replaced backend, or a moved/resized window can never be laundered into a fresh
         frame — including when the shown observation *also* aged out (the combined case R2
         names).
      4. **click only — the target itself must be unchanged (R3).** The registry keeps, for each
         issued observation, the exact JPEG bytes sent and their quality. The fresh frame is
         encoded at the same scale and quality; both are decoded; around the click point a
         33x33 transport-pixel patch (clipped to the image) is compared **in colour, per pixel,
         per channel**: allowed only if max abs difference ≤ `PATCH_MAX_DIFF = 12` and mean
         ≤ `PATCH_MEAN_DIFF = 1.0`. A 33x33 patch is ≈ 26 frame px at 1557 width — larger than a
         Minecraft inventory slot (18 GUI px × gui scale), so an item appearing, leaving or
         changing in the clicked slot, or a button changing state, fails the check. Also
         required: whole-image colour MAD ≤ `GLOBAL_MAD = 2.0` (catches a menu opening or
         closing elsewhere). No global aggregate is used as the authorization for the target
         itself. Encoding is deterministic for identical pixels; the thresholds only absorb
         the rare re-render jitter, and are confirmed live (Verification).
         Residual risk, stated: a small change *outside* the patch that changes what the click
         means without moving the global MAD (a tooltip elsewhere, a counter) is not detected.
      5. The action is then built on the fresh observation and goes through the normal
         `Arbiter.submit` path (full `evaluate()` at submission and at press time). Response adds
         `"bound_to": "shown" | "fresh"` and, for clicks, `"patch_max"`, `"patch_mean"`,
         `"global_mad"`. Registry memory: JPEG bytes are kept only while the record lives
         (capacity 256, TTL 5 s — ≈ 256 × ~80 KB worst case ≈ 20 MB).
      If the shown observation is still fresh, it is used directly (`bound_to: shown`) and
      steps 2–4 are skipped.
    - key / press / look / sequence: the same provenance rules (1–3), no pixel check — in the
      world the screen is never still. Trade-off, stated: the decision was made on a frame up to
      one model turn (≤ 5 s, the registry TTL) old, while target, session, geometry and
      preemption are proven unchanged since that frame.
    - `"strict": true` on the MCP tool sends no `rebind`: shown observation or nothing.
    - The MCP tool result reports `bound_to` and, when nothing was dispatched, returns the fresh
      image with `isError: true` and the reason ("screen changed at the click point; decide
      again", "preempted since you looked", …). A refusal is never retried automatically.
  - Project `.mcp.json` registering `gamelens` for Claude Code, and a README snippet for Codex
    (`~/.codex/config.toml` `[mcp_servers.gamelens]`). The token is passed by env var reference,
    never written into a committed file.

### Part B — background input (probe first; build only what the probe proves)

- **B1. `tools/bg_input_probe.py`.** Posts `WM_KEYDOWN/UP` (with scancode, repeat and transition
  bits), `WM_MOUSEMOVE`, `WM_LBUTTONDOWN/UP` to the target HWND with `PostMessageW`, while the
  game is **not** foreground, and scores each case with churn and `screens.in_world()` on
  frames from the running GameLens (`/frame.jpg`, agent token — the probe never arms and never
  uses `SendInput`). Preconditions it **checks and refuses without**: the capture backend
  reported by `/state` is `wgc` or `printwindow` (never `mss` — review R4: MSS copies desktop
  pixels at the game's rectangle, so with a window on top it would score *that* window).
  **Forced backend, no failover (review R6).** B1 runs before B2 exists, so B2's restricted
  fallback order cannot protect it: `CaptureSupervisor` can replace PrintWindow with MSS after
  a 0.5 s stall, mid-hold, and `/frame.jpg?after=` would serve the replacement's frame. The
  probe therefore requires GameLens started with `--backend printwindow` or `--backend wgc`
  (`CaptureSupervisor(forced=...)` sets `_order = (forced,)` and never fails over,
  `capture.py:791,879`), and reads `/state` capture `backend` and `session_id` before the
  case's pre-check frame and again after its after-frame; any change of backend or session
  within a case marks that case and every later one `inconclusive`. A unit test fakes a
  session change during a hold with an unchanged, hotbar-visible frame and asserts
  `inconclusive`.
  **The metadata contract this needs (review R7).** `CaptureSupervisor.stats()` has
  `session_id`, but `GameLens.state()` rebuilds the capture dict and drops it, and the forced
  backend is not exposed at all. So: `CaptureSupervisor.stats()` gains `forced_backend`
  (`"wgc"`/`"printwindow"`/`"mss"` or `null`), and `GameLens.state()["capture"]` carries
  `session_id` and `forced_backend` through. The probe refuses to run when either key is
  missing, when `forced_backend` is null (fallback possible), or when it is `"mss"`. Tested
  through the real `GameLens.state()` and `/state` serialization (not a mock of it), alongside
  the session-change test.
  - **Every case is isolated (review R5).** Cases run least-disruptive first — baseline,
    mouse move, W held 1 s, left hold 1.3 s at crosshair, E last — and each one re-checks the
    backend, that the game is **not** foreground, and `in_world()` on a fresh frame
    immediately before injecting (so a paused world is detected, not scored — the exact
    confound of the 2026-09-22 run); posts its messages; posts the matching up-messages in a
    `finally`; fetches the after-frame with `after=`/`frames=3`; then verifies the starting
    screen is back (`in_world()` true). The E case attempts one restoring E press. If a
    pre-check or the restore check fails, that case and every later one are recorded
    `inconclusive`, never `supported`/`unsupported`. A case is `supported` only if churn exceeds
    baseline by a margin **and** the case-specific evidence agrees (E: `in_world()` false
    after the first press).
  - Output: a JSON result and before/after JPEGs, in a directory given on the command line.
  - `pauseOnLostFocus` is the Owner's game setting. The probe does **not** edit `options.txt`.
    The Owner toggles it in-game (F3+P) or approves an edit while the game is closed; the probe
    only reports it.
- **B2. `--input window` backend, conditional on B1.** If B1 shows keys and/or clicks take effect
  unfocused, `InputExecutor` gains an injector strategy: `SendInputInjector` (today's code,
  default) and `WindowMessageInjector` (PostMessage to the target HWND, client coordinates from
  `Geometry` for clicks). In window mode `SafetySupervisor.check()` does not require
  `NOT_FOREGROUND` (input cannot reach any other window) but still requires the target to exist
  and **not be minimized** (new `Denial.TARGET_MINIMIZED`).
  **Capture must be occlusion-safe in window mode (review R4).** Without the foreground
  interlock the game may be covered by the Owner's own windows, and the MSS backend would then
  serve images of those windows with valid, fresh provenance. In window mode: `--backend mss`
  is refused at startup; the capture supervisor's fallback order is `(WGC, PRINTWINDOW)` only;
  and the guard denies with `Denial.CAPTURE_UNSAFE` whenever the active backend is MSS or there
  is none. Tests: forced `mss` refused, a runtime fallback that would reach MSS stops at
  PRINTWINDOW, and a guard check with an MSS backend denies. Every step kind the probe did not
  prove is refused with a named denial (`Denial.UNSUPPORTED_BY_INPUT`) — in particular `look`
  is expected to be unsupported: SDL relative mouse mode reads raw input, which cannot be posted.
  `/state` reports `input: {"backend": ..., "unsupported": [...]}`.
  If B1 shows nothing works unfocused even with the world unpaused, **B2 is not built**; the
  finding goes in the vault and the README with the options that do give the game its own input
  queue (Windows Sandbox — available on Win10 Pro — a VM, or a second machine), which are
  system-setup decisions for the Owner, not something the harness does.

## Non-goals

- No change to arming, tokens, the kill switch or the operator/agent split. The MCP server cannot
  arm, go live or stop.
- No elevation, no raw-input spoofing, no hooking the game process, no mods.
- No click steps inside a `sequence` (coordinates stay on `click`, which has bounds checks).
- No automatic editing of the Owner's Minecraft settings.
- Not replacing `play.py`/`bot.py`; they keep working unchanged.

## Key trade-offs

- Hand-rolled MCP instead of the `mcp` SDK: ~200 lines, stdlib only, no install into the Owner's
  venv. Cost: must track protocol shape by hand; mitigated by tests that drive it through real
  stdin/stdout pipes with the exact messages Claude Code sends.
- Server-side rebinding with a local patch check (A3). Cost: a click can land up to 5 s after
  the frame it was chosen on, when provenance and the pixels at the click point are unchanged;
  a change elsewhere that alters the click's meaning without moving the global MAD is the
  residual risk. The alternative — refusing every expired click — makes clicking impossible at
  model speed.
- Relaxed per-step freshness inside a sequence (A1). Cost: a sequence can act up to 5 s past its
  frame. Bounded, and every "world changed" rule except age still applies at each press.
- Probe-gated B. Cost: part B may produce only a documented negative. That is a result, not a
  failure; the 2026-09-22 negative was wrong for a reason that is now visible.

## Assumptions (with sources)

- Minecraft Java 26.3 demo, window class `SDL_app` — vault note 2026-09-22.
- `pauseOnLostFocus:true` — `%APPDATA%\.minecraft\options.txt`, read 2026-09-24.
- Venv Python 3.11.2, `fastapi 0.115.0`, `httpx 0.28.1`, no `mcp` package — `pip list` 2026-09-24.
- Rate default 10/s, bucket capacity = rate — `gamelens/safety.py:84-125`, `__main__.py`.
- Freshness rules and press-time revalidation — `gamelens/arbiter.py:200-285`.
- Minecraft is not running now (no java process, 2026-09-24): **B1 cannot run until the Owner
  opens the game**; everything else is provable without it.

## Verification

- `.venv/Scripts/python.exe -m pytest tests/ -q` — all existing tests (206 per vault) plus new:
  - `tests/test_sequence.py`: builder validation (each 400 case), auto-release order, new-input
    count vs capacity, `_expected_duration`, freshness rule applies only at first new input while
    geometry/preemption still refuse later steps (fake capture/geometry), executor releases held
    keys when denied mid-sequence (existing fake-send pattern), HTTP edge rejects malformed
    sequences before `submit` is called.
  - `tests/test_mcp.py`: spawn `python -m gamelens.mcp` against a stub HTTP server (threaded
    `http.server` that mimics `/state`, `/frame.jpg`, `/act`), send `initialize` →
    `notifications/initialized` → `tools/list` → `tools/call` over real pipes; assert protocol
    shape, image content, `isError` on 409, nothing on stdout but JSON-RPC; no shown
    observation → no `/act`; `rebind: true` sent by default and not with `strict: true`; a
    refusal is surfaced with a fresh image and never retried (exactly one `/act` per call).
  - `tests/test_rebind.py` (server side, fake capture/geometry/registry): shown observation
    fresh → `bound_to: shown`; expired + all provenance equal + identical image → `fresh`;
    **expired and preempted together → `PREEMPTED`, not rebound**; expired + backend replaced
    → `RETIRED_SESSION`; expired + geometry generation bumped → `GEOMETRY_MOVED`; size change
    → refused; registry record evicted/aged out → `STALE_OBSERVATION`, fail closed; click where
    only a 12x12 transport-pixel region **at the click point** changed (global MAD well under
    2.0) → refused on the patch; the same change 60 px from the click → allowed (the residual
    risk this represents is named in the test's docstring); a colour-only change at the click
    point (equal greyscale) → refused; key with preemption → refused; key with only age →
    rebound.
  - Live calibration of `PATCH_MAX_DIFF` / `PATCH_MEAN_DIFF` / `GLOBAL_MAD`: consecutive frames
    of a static menu stay under all three; hovered vs unhovered button at the button, and an
    inventory slot before/after an item moves, exceed the patch limits. Numbers recorded in the
    plan; if a real change passes, the limits are lowered until it does not.
  - If B2 is built: `tests/test_window_input.py` with a Tk window that records the messages it
    receives (same approach as `tools/moving_window.py`), proving keys/clicks arrive with correct
    lParam and client coordinates while another window is foreground; safety tests for
    `TARGET_MINIMIZED` and `UNSUPPORTED_BY_INPUT`.
- Mutation check on the new rules (flip the auto-release, the freshness gate, the capacity
  check; the suite must fail each time).
- Live (needs the Owner's game open, GameLens armed and live): one MCP `gamelens_act` sequence
  `key_down w, look +200 x4 with waits, key_up w` shows the player walked and turned in the
  returned frame; B1 probe run with the world unpaused, results recorded.
- Vault: session log + `Open Items.md` updated (GL-039), per the GameLens logging rule.
