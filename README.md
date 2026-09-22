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

```bash
.venv/Scripts/python.exe -m gamelens --list
```

```bash
.venv/Scripts/python.exe -m gamelens --target "Your Game Window"
```

GameLens prints two tokens to its console and opens on `http://127.0.0.1:8777/`. Paste the
**operator** token into the dashboard. Nothing is injected until you press **Arm** and then
**Go live** — two separate steps, on purpose.

**Kill switch: F12 or Pause.** It latches; restart the process to clear it.

## Safety model

Everything here fails closed. The failure being designed against is not a malicious agent —
it is *something broke and the system kept clicking anyway*.

- **Dry-run by default.** Actions are logged and drawn on the dashboard overlay, not injected.
  Check that the overlay marks land on the real buttons before going live.
- **Arming is operator-only.** There are two tokens: the operator token can arm, go live and
  stop; the agent token can only propose actions. Model output can never grant itself
  authority.
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
- **The session tokens stay on the console.** Two secrets, printed once, for one operator on
  one machine, over loopback. Anything that makes them reachable from another device needs a
  transport that is actually authenticated, not a longer token — so that is the change to make
  if it is ever wanted, rather than widening this.
- **The buffer pool depth stays at 4.** Measured rather than assumed: four concurrent clients
  pulling as fast as the server would serve, 1750 fetches in 8 seconds, zero exhaustion. Leases
  are short everywhere by design, including across the model call.

## Known limitations

These are real and not worked around:

- **WGC target identity is evidence, not proof.** `windows-capture` 1.4.2 binds by window
  *title* and exposes no way to read back the HWND it bound. GameLens requires sole title
  ownership before binding and re-verifies continuously, but ownership can still change in
  the instant between the last check and the native bind. Use `--backend printwindow` when
  you need HWND-exact capture.
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

83 tests covering the interlocks, the arbiter's rejection rules, the coordinate transforms,
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

## Provenance

The plan was hardened through five rounds of independent Codex review, then the finished
implementation was reviewed in a fresh session (`PLAN.md`, `PLAN-REVIEW-LOG.md`). Thirty-four
findings were raised across both phases; all thirty-four were accepted after verification and
thirty-three are fixed. Several were confirmed by reading the installed dependency's source or
by running the code rather than taken on faith — including the missing HWND selector, the
duplicate frame delivery, and a buffer-lease leak that emptied the frame pool after four
clicks.
