# GameLens

Low-latency Windows capture and input harness that lets an AI agent see a **native PC game
window** and act in it.

It is not an OBS clone, and that is the point. OBS exists to *encode* — to a file, to RTMP —
and an encoder is pure added latency in a control loop. GameLens never encodes for its own
sake: frames go straight from the GPU into memory where the agent reads them.

## Why the architecture looks like this

| Stage | Cost |
|---|---|
| WGC capture | 5–16 ms |
| Encode + transport (dashboard only) | 5–20 ms |
| **Model inference** | **300–2000 ms** |
| Input injection | 1–5 ms |

Capture was never the bottleneck. Making it faster than the reflex tier needs buys nothing,
so the agent is split in two: a **fast reflex tier** (pixel probes, template matching) for
anything that must happen inside 100 ms, and a **slow vision tier** at 2–5 fps for judgement.

Measured on this machine (Windows 10 19045, RTX 4060), capturing a 1557×873 window:

```
backend    : wgc          118.7 fps      frame age  0 ms
backend    : printwindow   33.0 fps      frame age 16 ms
```

## Quick start

### Multiple game clients

After ongoing gameplay finishes, run `./tools/install_multi_client.ps1` to
configure ten named sessions `client-01` through `client-10` (ports8777–8786) and install
matching Desktop/Start menu shortcuts. Each app selects its own game window;
non-default ports appear in the window title. No game or capture starts from
the installer. Reload Codex, call `gamelens_clients`, then specify `client`
on state/see/act/recording calls. Input still uses the selected foreground game.
See [multiple-client usage](docs/MULTI_CLIENTS.md) for configuration and limits.

### Record the selected game

Use **Start recording**, then **Stop recording** in the GameLens window.
Video-only MP4 files save to `%USERPROFILE%\Videos\GameLens`; **Open recordings
folder** opens that location. Recording uses the selected capture window at
30 FPS, works while disarmed, and starts no input. It runs on a separate thread
and finalizes the file when stopped or when the owning app closes. Capture
session/size changes stop the clip with a visible message. Audio is not recorded.

### Codex plugins and MCP

The local marketplace packages two plugins: **GameLens** (MCP and native game
control skill) and **GameLens GodsArena** (saved Marathon route and lessons).

```powershell
./tools/install_plugins.ps1
```

Reload Codex after installation. The installer preserves or creates the single
standalone MCP registration used by the current Codex runtime. Bundled MCP
configs are included for compatible hosts. It connects to the local GameLens
app with its encrypted agent capability and exposes `gamelens_state`,
`gamelens_see`, `gamelens_act`, `gamelens_recording`, `gamelens_session`, and
read-only `gamelens_profile`. The named-client provider also adds `gamelens_clients`. The profile tool
works without a running game and cannot read arbitrary paths. The GodsArena
companion adds the learned workflow without a second MCP connection.

Install starts no capture, input or race. Open GameLens and select the game;
Owner-authorized agents can Arm/Go live with `gamelens_session`. Plugins require this Windows project and `.venv`.
Rerun the installer after moving the project. It compares the existing standalone
registration with the current paths and stops on a mismatch. Review the proposed
paths, then pass `-RepairRegistration` to explicitly update a recognized stdio
registration. Custom environments require manual review; named-client configuration
is preserved and must still exist. Keep only one enabled connection for this server.

Sharing the repository for GodsArena Marathon use? Follow the
[setup and profile-matching guide](docs/SHARED_MARATHON_SETUP.md). Best verified
clean time is867.060 seconds; other PCs/characters and a700-second run require
their own live proof.

### Desktop app (Windows)

Install the desktop dependency and shortcuts once:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
./tools/install_desktop.ps1
```

Open **GameLens** from Desktop or Start menu. It has its own native window;
Microsoft Edge WebView2 Runtime is required. When no server is running, choose
the game window and press **Open GameLens**, then **Arm** and **Go live** yourself.
Closing that app window stops its capture server and releases inputs.

If GameLens is already running on port 8777, the app connects and leaves it
running when closed. Native desktop login is automatic using a separate
Windows-encrypted operator handoff, so the app needs no manual token popup.
Old servers without that handoff attach with agent access until restarted.
Browser access still requires a token; its popup can be closed. MCP clients
continue to use agent capability. Owner-authorized agents can Arm/Go live through
`gamelens_session`; no second owner click is needed. See [agent session controls](docs/AGENT_SESSION.md).

```powershell
.\.venv\Scripts\pythonw.exe -m gamelens.desktop
```

The shortcut uses this project's `.venv`; keep the project in its installed
location or rerun the shortcut installer after moving it. The CLI remains usable.

```bash
.venv/Scripts/python.exe -m gamelens --list
```

```bash
.venv/Scripts/python.exe -m gamelens --target "Your Game Window" --no-agent
```

GameLens prints two tokens to its console and opens on `http://127.0.0.1:8777/`. Paste the
**operator** token into the dashboard. Nothing is injected until you press **Arm** and then
**Go live**, or an authorized agent calls those two explicit session actions.

**Kill switch: F12 or Pause.** It latches; restart the process to clear it.

For Codex or Claude driving through MCP, use `--no-agent`: no separate paid vision API is
needed. The dashboard shows the current access role, foreground state, blocked-input reason,
executor results and snapshot retention. Authenticated operators and agents can Arm,
Go live or Stop; startup stays disarmed/dry-run and enabling sends no game input.
`sent` means input was injected; inspect the next image to confirm the game actually changed.

## Safety model

Everything here fails closed. The failure being designed against is not a malicious agent —
it is *something broke and the system kept clicking anyway*.

- **Dry-run by default.** Actions are logged and drawn on the dashboard overlay, not injected.
  Check that the overlay marks land on the real buttons before going live.
- **Session control follows the owner's task.** Both authenticated roles can arm, go live
  and stop through explicit calls. The agent token remains separate from the operator
  credential; window enumeration and opening the recordings folder remain operator-only.
- **Every interlock must pass**, independently: armed, not killed, target window in the
  foreground, within the rate limit, and the watchdog both *alive* and beating recently.
  An exception anywhere inside the guard denies.
- **No gap between approval and press.** Authorization and injection happen under one
  boundary, so a kill landing mid-sequence cannot be followed by the click it approved.
- **Held keys are always released.** If a kill lands while a key is down, cleanup releases
  exactly what was pressed — never more, never a new press. Denying the key-up too is how a
  game ends up with W held forever.
- **Loopback is not authorization.** Any local process can reach 127.0.0.1 and any page you
  open can POST to it, so the dashboard is served with no credential in it, and `Origin` and
  `Host` are checked.
- **Nothing is reported as done until it is.** An action is logged when it is queued and
  resolved when the executor says what became of it. The two differ often enough to matter: a
  refusal at press time used to leave a green line in the log and a green overlay mark on the
  exact spot the click did not land.

## Layout

| Module | Job |
|---|---|
| `dpi.py` | Establish **and verify** per-monitor DPI awareness, per thread |
| `windows.py` | Enumerate and resolve targets; refuses ambiguous ones |
| `coords.py` | frame → client → screen mapping with a geometry generation counter |
| `capture.py` | WGC / PrintWindow / mss backends, buffer pool, health supervisor |
| `input.py` | SendInput via ctypes, interruptible serialized executor |
| `safety.py` | Kill latch, foreground guard, rate limit, watchdog liveness |
| `arbiter.py` | The single gate; rejects actions whose world has moved on |
| `server.py` | FastAPI: MJPEG, state, token-authenticated control |
| `agent.py` | Fast reflex tier + slow Claude vision tier |

## Writing reflexes

Reflexes run on every distinct frame, so they must be cheap. Anything needing a full-
resolution scan belongs in the vision tier.

```python
from gamelens.agent import Agent, pixel_reflex, template_reflex

agent = Agent(
    lens,
    goal="clear the level",
    reflexes=[
        pixel_reflex("health-low", x=120, y=940, bgr=(40, 40, 200), click_at=(300, 880)),
        template_reflex("continue-button", "templates/continue.png", threshold=0.9),
    ],
)
```

When a reflex fires it **preempts** the vision tier: whatever the model is currently
reasoning about has just been invalidated by the reflex's own action, so its answer is
discarded on arrival.

## Decisions, and the reasons

These were open questions during the build. They are answered, not deferred.

- **GameLens will not launch itself elevated.** UIPI blocks injection into a window owned by an
  elevated process, and the obvious fix — run GameLens elevated too — buys one target and gives
  away the boundary. An elevated injector can drive UAC prompts, security dialogs and every
  other window on the desktop, so a coordinate bug stops being a misclick in a game. The
  current behaviour is a `SendInput` short count, which fails closed and says exactly why.
- **Session credentials stay local.** Tokens are printed for the local operator and have
  separate Windows-encrypted handoff files. MCP uses only the agent capability. Anything that makes them reachable from another device needs a
  transport that is actually authenticated, not a longer token — so that is the change to make
  if it is ever wanted, rather than widening this.
- **The buffer pool depth stays at 4.** Measured rather than assumed: four concurrent clients
  pulling as fast as the server would serve, 1750 fetches in 8 seconds, zero exhaustion. Leases
  are short everywhere by design, including across the model call.

## Known limitations

These are real and not worked around:

- **WGC target identity is evidence, not proof, on the pinned `windows-capture` 1.4.2.** It
  binds by window *title* and exposes no way to read back the HWND it bound. GameLens requires
  sole title ownership before binding and re-verifies continuously, but ownership can still
  change in the instant between the last check and the native bind; and a game that retitles
  its window (Java: "Minecraft 26.3" -> "Minecraft 26.3 - Singleplayer") loses WGC until
  restart. Use `--backend printwindow` when you need HWND-exact capture. GameLens binds by
  HWND automatically when the installed library offers `window_hwnd` (2.0+).
- **`windows-capture` can crash natively when a WGC session restarts** -- an access
  violation, no Python traceback. Measured on the Hyper-V VM: 2.0.1 on 2 of 7 Java world
  reloads (first in `GraphicsCapture.dll` after it was unloaded, then, with that DLL pinned,
  inside `windows_capture.pyd`), and 1.4.2 inside `windows_capture.pyd` when an Enhanced Session
  connect starved WGC and it was tried again. So WGC runs in its own worker process, like
  PrintWindow: a crash costs the worker, the log says `wgc worker exited (code 0xc0000005)`,
  and capture fails over to PrintWindow and later promotes back. GameLens itself stays up. The
  pin stays at 1.4.2: 2.0.1 has `window_hwnd` and delivers each frame once, but it has not been
  re-measured since the move.
- **Games using RawInput with `RIDEV_NOLEGACY`, or anti-cheat, may ignore `SendInput`.** It
  is a documented user-mode API and GameLens does not try to defeat anything. No kernel
  drivers, no evasion. Whether automating a given game is permitted is your call.
- **UIPI blocks injection into elevated windows** unless GameLens is elevated too. This
  surfaces as a `SendInput` short-count error rather than silent nothing.
- **`windows-capture` 1.4.2 delivers padded frames twice** (`__init__.py:252` and `254-257`).
  GameLens deduplicates on the native timespan. On this machine every frame is a padded one,
  so without that the fps counter would read double and the reflex tier would process each
  image twice.
- **Hiding WGC's yellow capture border needs Windows 11.** On Windows 10 the toggle throws,
  so GameLens does not ask for it.
- **Windows emits no move event for a move to the pixel the cursor already occupies.** Measured
  against a Tk window: an absolute `SendInput` move to the current cursor position produced no
  motion event at all, only the press that followed. An application that tracks the pointer
  through move events -- GLFW games among them -- is then pressed at a coordinate it was never
  told about, and ignores it, while `SendInput` returns a full count. GameLens steps one pixel
  aside first, in the same `SendInput` call, so the event is always delivered.
- **The PrintWindow backend needs a `__main__` guard.** Windows spawns rather than forks, so
  the worker process re-imports the caller's `__main__`. `python -m gamelens` is fine; embedding
  GameLens in an unguarded script is not, and the supervisor will tell you so rather than
  letting it look like a stalled game.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
```

609 tests covering the interlocks, the arbiter's rejection rules, the coordinate transforms,
per-thread DPI, the observation registry and transport scaling, the HTTP boundary, and the
outcome reporting that tells acceptance apart from execution.

**What tests cannot establish:** that a click actually hit a button. Those were run against
a real game rather than asserted:

- **Kill during a dwell.** The cursor arrived on the button, the kill landed inside the settle,
  `/act` returned `409 {"outcome":"denied","detail":"kill switch latched"}`, the executor showed
  `executed 1 / denied 1`, and the game stayed on the menu it was on. Also covered by
  `tests/test_killswitch.py`, including the hotkey path itself — a real key event through
  `SendInput`, picked up by the watchdog's `GetAsyncKeyState` poll.
- **Dry-run overlay accuracy.** The overlay crosshair landed on the button the agent meant.

Still open, and it needs hardware rather than effort: the coordinate round-trip on a
**negative-origin monitor**. `tools/coord_probe.py` is the one command to run when a display
sits left of or above the primary; it reports zero error here and says plainly that a
single-monitor run cannot prove the case.

## Acting on a frame over HTTP

Every image the server issues carries an `X-GameLens-Observation` id. Send that id back with
your coordinate; the server resolves the rest from the record it made when it handed you the
picture.

```bash
curl -s -D - -o frame.jpg -H "X-GameLens-Token: $AGENT_TOKEN"   http://127.0.0.1:8777/frame.jpg
```

```bash
curl -s -X POST -H "X-GameLens-Token: $AGENT_TOKEN" -H 'Content-Type: application/json'   -d '{"observation_id":"<from the header>","x":400,"y":300}' http://127.0.0.1:8777/act
```

The reply reports two different things, because they routinely disagree:

```json
{"verdict": "ok", "outcome": "denied",
 "detail": "target window is not in the foreground", "action_id": 6}
```

`verdict` is the arbiter's: the world was still the one you reasoned about. `outcome` is the
executor's: what happened when the press actually came up in the queue. An action can be
accepted and then refused, because between the two sit a queue, a settle and a hold, and every
interlock gets another vote at press time. HTTP 200 means accepted *and* not refused; 409 means
one of the two said no. `"outcome": "pending"` means the request stopped waiting -- never that
it worked.

You never send a timestamp, a geometry version, or a scale factor — a caller cannot know
whether capture has stalled or a reflex has preempted since it got the picture, so it does not
get to assert its own freshness. Coordinates are in the pixels of the image you received; the
transport downscale is recorded server-side and applied for you.

## Agent controls: MCP tools and sequences (GL-039)

Any MCP client — Claude Code, Codex, another agent — can drive GameLens with three tools
instead of hand-written HTTP calls:

| tool | what it does |
|---|---|
| `gamelens_state` | armed / live / killed, capture backend and fps, target window |
| `gamelens_see` | the newest frame, as an image |
| `gamelens_act` | one action, then the frame after it — **one call is act + see** |

The server is `python -m gamelens.mcp`, a stdio MCP server that is an HTTP client of a running
GameLens. It holds **the agent token only**. Capture startup automatically writes it to
`%LOCALAPPDATA%\GameLens\agent-8777.token`, encrypted with Windows DPAPI for the current user;
the MCP reads it on each request and derives the filename from its configured HTTP port.
Normal capture shutdown removes its own credential. Restarting capture rotates it without
restarting MCP. The operator token has a separate Windows-encrypted desktop handoff and is never
used by the MCP client. Explicit `GAMELENS_AGENT_TOKEN` takes precedence;
`--token-file` still accepts a supplied plaintext agent token file. Authenticated
agents may explicitly Arm, Go live and Stop for an owner-authorized task; see
[session controls](docs/AGENT_SESSION.md). The token alone does not establish the
owner's authorization for a task.

Claude Code picks it up from this repo's `.mcp.json`. For Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.gamelens]
command = "B:/AI_Agent_folder/GAME VIDEO/.venv/Scripts/python.exe"
args = ["-m", "gamelens.mcp"]
cwd = "B:/AI_Agent_folder/GAME VIDEO"
startup_timeout_sec = 20
tool_timeout_sec = 45
```

Keep `cwd` on the updated canonical checkout containing `gamelens/mcp.py`. After adding the
server, reload/restart it in the app's MCP settings; an already-running chat may still have its
previous tool list. Start capture with the quick-start command, paste the console's operator
token into the dashboard, and check state/see before acting. Never put either token in TOML.

**`kind: "sequence"`** puts overlapping steps in one action, so "walk while turning" is one
call instead of a turn after the walk has already stopped:

```json
{"kind": "sequence", "steps": [
  {"do": "key_down", "key": "w"},
  {"do": "look", "dx": 200, "dy": 0}, {"do": "wait", "ms": 250},
  {"do": "look", "dx": 200, "dy": 0}, {"do": "wait", "ms": 250}
]}
```

Steps: `key_down`, `key_up`, `tap` (`ms`), `button_down`, `button_up`, `click` (`button`,
optional `x`,`y`), `move` (`x`,`y`), `scroll` (`clicks`, `horizontal`), `look` (`dx`,`dy`),
`wait` (`ms`). Everything pressed is released at the end. At most 64 steps and 5 s of waits,
and no more new inputs than `--rate` allows at once. A sequence is judged for age at its first
press; every later press still re-checks target, capture session, geometry, preemption and
every safety interlock.

### Any game, not only Minecraft (GL-040)

Nothing in the controls knows which game is running. What a game is played with:

| game style | how |
|---|---|
| first/third person (camera follows the mouse) | `look` to turn, `press` to fire/use where the crosshair is, keys to move |
| visible cursor (strategy, RPG menus, card, point-and-click) | `click` at image pixels; `move` to hover; drag and modifier-clicks as a sequence |
| weapon/hotbar wheel, zoom, list scroll | `kind: "scroll"` or a `scroll` step; negative is down |

```json
{"kind": "sequence", "steps": [
  {"do": "move", "x": 210, "y": 340}, {"do": "button_down"},
  {"do": "move", "x": 520, "y": 340}, {"do": "button_up"}
]}
```

is a drag (box-select, move an item); `[{"do":"key_down","key":"shift"},{"do":"click","x":210,"y":340}]`
is a shift-click. Every `move`/`click` point goes through the same bounds and geometry checks
as a click, and one point outside the window refuses the whole sequence before any of it runs.
With `rebind`, **every** point must still look as it was shown -- a drag whose drop point
changed is refused even if the grab point did not.

**Keys** (single `key` action): `a`-`z`, `0`-`9`, `f1`-`f11`, `space`, `enter`, `tab`,
`escape`, `backspace`, `shift`/`ctrl`/`alt` and `rshift`/`rctrl`/`ralt`, arrows, `insert`,
`delete`, `home`, `end`, `pageup`, `pagedown`, `numpad0`-`numpad9`, `multiply`, `add`,
`subtract`, `decimal`, `divide`, and punctuation by name (`minus equals lbracket rbracket
backslash semicolon quote comma period slash backtick`) or by character. Never: the Windows
and menu keys, F12 and Pause (the kill switch), PrintScreen and the lock keys. In a
**sequence**, all of these except `alt`, `ralt` and `escape` -- those are what Alt+Tab, Alt+F4
and Ctrl+Esc need, and a chord the shell acts on cannot be undone by a later denial.
**Buttons:** `left`, `right`, `middle`, `x1`/`mouse4`, `x2`/`mouse5`.

**Checked at every press** (GL-040 inspection), on top of the interlocks:
- a button press or a wheel notch needs the real cursor over the game window -- a `look` in a
  windowed game can carry an unlocked cursor off the edge (`POINTER_OFF_TARGET`);
- a key press is refused while either Alt or Windows key is held by anyone but GameLens, and
  escape also while Ctrl is (`FOREIGN_MODIFIER`) -- your Alt plus an agent's F4 is Alt+F4;
- a release that Windows rejected stops the action and blocks all new input until a retry
  releases it (`UNRELEASED_INPUT`).

Not closed: shift tapped five times is Windows's Sticky Keys prompt, if the Owner has it
enabled; it takes the foreground, so the interlock stops the input that follows. A modifier
pressed on the physical keyboard in the microseconds between the check and the press is not
seen. A `look` while a button is held can move an unlocked cursor off the window mid-drag;
the release is never withheld, and goes to the window that captured the mouse (normally the
game). Through MCP, an `/act` whose answer is lost is reported as possibly carried out -- never
retried.

**Rebinding.** A model's turn is longer than the 0.8 s an observation lives, so an agent
nearly always acts on an expired frame. `gamelens_act` sends `"rebind": true`, and the server
carries the action to the newest frame **only if nothing but time changed**: same target,
capture session, geometry, size and preemption counter, compared between the two records.
For a click, the 33x33 pixels around the click point must also still match the image the
agent was shown, in colour (and the whole image roughly), or it answers `SCREEN_CHANGED`
and the agent gets the current image to decide on again. It never retries. Residual risk,
stated: a small change *away from* the click point that changes what the click means is not
seen. `strict: true` turns rebinding off for a call.

Agent `/frame.jpg` snapshots are retained for up to **120 seconds**, at most **16 snapshots**.
Retention preserves the original pixels and provenance for comparison; it does not make an
old frame fresh. The original arbiter deadlines, per-press guards and rebind checks remain.
Dashboard MJPEG observations have a separate bounded pool (256 records, 5 seconds), so a busy
preview cannot evict an agent's snapshot. Expired/evicted records still fail closed. A refusal
returns a current image: inspect it and decide again; never automatically repeat an action.

**Not using your mouse and keyboard.** Everything above still injects with `SendInput`, which
is global, and still needs the game in the foreground. Whether a game accepts input posted to
its window while unfocused is per game. Minecraft Bedrock (2026-09-24): posted keys work,
posted mouse clicks do not, and it pauses on focus loss and grabs the cursor when resumed --
so on a shared desktop it fights the Owner either way. The durable answer is a separate
machine or VM (`tools/vm/new-test-vm.ps1`). `tools/bg_input_probe.py` is a Minecraft
Java-only experiment on the same question.

### Animated NPCs and translucent menus

Ordinary clicks retain the exact pixel-patch check. For a target with a visible yellow
name or command label, an agent can explicitly provide a small fixed-position anchor:

```json
{"kind":"click","x":583,"y":322,"rebind":true,
 "anchor":{"x":535,"y":288,"width":125,"height":16,"color":"yellow"}}
```

Coordinates refer to the issued image. The label must be 20–256 pixels wide, 8–48 high,
and within 40 pixels of the click. GameLens verifies identifying yellow glyphs from the
stored image against a fresh frame at that same location. Nearby animation may change;
a changed, hidden, moved or uninformative label refuses with `TARGET_CHANGED`. No searching,
automatic retry or threshold override. MCP passes `anchor` inside `action` with `strict:false`.
Use ordinary clicks for inventory items and targets without such a label.

Target/session/geometry/preemption, foreground, pointer occlusion, watchdog, rate and kill
guards remain. If the human moves the cursor during an agent click's settle time, the
executor refuses the press even when the cursor is still inside the game. Reobserve before
resuming. A video covering the destination also refuses; GameLens never steals focus to
continue. This improves target verification, not desktop isolation: simultaneous Owner
mouse/keyboard use requires the game and GameLens in a separate PC/guest Windows session.

## Provenance

The plan was hardened through five rounds of independent Codex review, then the finished
implementation was reviewed in a fresh session (`PLAN.md`, `PLAN-REVIEW-LOG.md`). Thirty-four
findings were raised across both phases; all thirty-four were accepted after verification and
thirty-three are fixed. Several were confirmed by reading the installed dependency's source or
by running the code rather than taken on faith — including the missing HWND selector, the
duplicate frame delivery, and a buffer-lease leak that emptied the frame pool after four
clicks.

## Safety review follow-through (October 1, 2026)

- Runtime shutdown latches Stop and releases held input before waiting for video
  finalization. A shutdown cannot be resumed by Arm/Live.
- Both MCP transports validate literal-loopback origins and refuse proxies and
  redirects. No credential is forwarded to another port/origin.
- Action responses include `completed_steps`, `injected_steps`,
  `last_completed_step` (zero-based) and `partial`. A denied/error result with
  `partial: true` means earlier input already reached the injector: inspect the
  game, never replay the entire sequence. Counts describe confirmed new-input
  sends, not proof of the game's response; a native send failure can still be
  ambiguous. Pending results have unknown counts, not a claimed zero.
- The executor admits at most 32 queued sequences by default and refuses excess
  submissions immediately. This bounds backlog; existing freshness and safety
  checks remain in force.
- `.github/workflows/windows-safety.yml` runs isolated safety/transport tests on
  Windows for pull requests or manual dispatch. It never launches a game or
  proves live gameplay, driver stability, or complete desktop acceptance.

These review changes were authored statically. Before merge, run the focused
regressions, existing suite and an owner-authorized disposable-window acceptance
test. Do not describe an unexecuted test as passing.

## Standalone setup checker (Phase 1)

`tools/check_setup.ps1` or `python -B tools/check_setup.py` provides read-only,
offline-by-default diagnostics without importing GameLens or starting capture.
An explicit `--connect` and selected client/origin permits one bounded `GET /state`.
Server-reported health is not frame verification, and the checker grants no input
authorization. MCP inspection requires an explicit supported host and config file.
See [the setup checker contract and limits](docs/SETUP_CHECKER.md).

The isolated `tests_setup/` tests are **authored, NOT RUN** for this implementation.
No installation, workflow dispatch, live-game test or later phase is included.
