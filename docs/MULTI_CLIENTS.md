# Named local GameLens clients

## Installed Windows setup

Run `./tools/install_multi_client.ps1` after ongoing gameplay ends. It creates
`profiles/clients.local.json` with **client-01 through client-10**, ports8777–8786,
installs matching Desktop/Start menu shortcuts and replaces the single GameLens
MCP registration with the named-client bootstrap. Reload Codex to load its seven
tools. The example file below uses names without hyphens; always discover the
actual configured names with `gamelens_clients`.

Slots need not run simultaneously and can target different games. Open only the
matching GameLens shortcut needed, select that game's window, then Arm/Go live
yourself or let the authorized agent call `gamelens_session` arm then live for that
client. The engine currently accepts up to16 configured slots; that is a config
bound, not a measured PC capacity. Each game's controls/learned profile must be
kept separate. GodsArena Marathon automation is not used for other games.

## Backend and configuration

This opt-in stdio entrypoint connects to multiple separately running GameLens
desktop sessions. It does not automatically start, attach, focus, arm, go live, stop, or restart
those sessions. The existing `python -m gamelens.mcp` and port 8777 setup stays as-is.

Copy `profiles/clients.example.json` to a local configuration and replace its
names/ports with your own separate sessions. The default example has **10 slots**,
`client01` through `client10`, using illustrative ports 8777 through 8786. It
contains no live window identity or credentials. Listing these slots does not
launch ten clients or change an existing session. Each desktop session needs a
unique `--port` and its own verified `--target`.
Start each desktop session through the existing desktop workflow or owner-directed
agent CLI, then inspect its target and choose Arm/Go live only for the instructed
task. See [agent session controls](AGENT_SESSION.md). These calls do not focus or
send game input, and cannot clear the emergency-stop latch.

From the project directory, run:

```powershell
& 'B:/AI_Agent_folder/GAME VIDEO/.venv/Scripts/python.exe' -m gamelens.multi_mcp --clients-file 'B:/AI_Agent_folder/GAME VIDEO/profiles/clients.example.json'
```

This command is a stdio MCP server: stdout carries JSON-RPC only. A future MCP
registration should launch that same Python executable with `-m`,
`gamelens.multi_mcp`, `--clients-file`, and the absolute configuration path, with
the project as its working directory. The module itself does not install or change
registrations. Review and activate it after ongoing gameplay/recording is finished;
avoid registering both providers with the same unnamespaced tool names.

The config shape is exactly `{"clients": [{"name": "client01", "url":
"http://127.0.0.1:8777"}]}`. Names are 1–32 lowercase letters, digits, hyphens or
underscores and start with a letter. There must be 1–16 clients. URLs accept only
canonical HTTP literal loopback origins `127.0.0.1` or `[::1]` with an explicit
port 1–65535 and optional trailing slash. Names and ports must be unique, including
across IPv4/IPv6, because encrypted session token files are keyed by port. Remote
hosts, localhost DNS names, credentials, token-file options, paths, queries,
duplicate JSON properties, default selectors and files larger than 64 KiB fail
closed before any HTTP request. Treat each local server as the existing trusted
GameLens HTTP service; all server-side authentication, foreground, observation,
arbiter and input-release guards remain in that service.
The per-client HTTP transport bypasses shared proxy settings and refuses redirects,
so a server cannot forward another request or its credential to a different origin.

Call `gamelens_clients` to list configured names and origins without contacting
any session. Supply `client` explicitly on every `gamelens_state`,
`gamelens_see`, `gamelens_act` and `gamelens_recording` call:

```json
{"client":"client01"}
{"client":"client02","quality":60}
{"client":"client01","action":{"kind":"key","key":"w"},"strict":true}
{"client":"client02","action":"status"}
```

These are argument examples for state, see, act and recording respectively;
input examples are not instructions to perform live input. Unknown, missing,
array or ambiguous selectors produce an error without dispatch. There is no
implicit selection, even with a single configured client, and no broadcast.
`gamelens_profile` remains the shared static curated guide and accepts no client.

Every routed result identifies its client. Each client remembers its own shown
observation and reads its own encrypted per-port agent token on every request,
so token rotation is picked up without an MCP restart. Shared
`GAMELENS_AGENT_TOKEN`, `GAMELENS_TOKEN_FILE` and `GAMELENS_URL` environment values
are deliberately ignored. Configuration cannot contain tokens or arbitrary token
paths. No token contents appear in inventory. Seeing client01 never enables an
action on client02: a first act on an unseen client returns that client's frame
without input, using the existing MCP behavior.

Recording status/start/stop routes only to the named session. Each session owns
its recorder and lifecycle; failures are not retried. Multiple simultaneous
captures/recordings are possible, subject to machine capacity. Input still uses
one shared foreground mouse and keyboard across all ten slots. Only the selected foreground target
can receive input; selecting a client does not focus it or provide independent
background input. Coordinate/observation guards and release of held inputs are
owned by the unchanged GameLens runtime.

Targeted verification:

```powershell
& 'B:/AI_Agent_folder/GAME VIDEO/.venv/Scripts/python.exe' -m pytest tests/test_multi_client.py tests/test_mcp.py -q
```

Tests use two temporary loopback HTTP stubs and isolated temporary session files,
plus actual stdio subprocess handshakes. They do not contact the running games.
Activation and actual two-game capture/foreground acceptance remain separate
from these routing checks.
