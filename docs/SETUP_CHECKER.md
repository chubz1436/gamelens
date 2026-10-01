# Standalone setup checker — Phase 1

**Implementation authored; all tests, syntax/compile checks and Windows acceptance checks are NOT RUN.** Review this branch before authorizing validation. These instructions do not authorize installation, live-game testing, workflow dispatch, merge or deployment.

Planning/source baseline: `e11f478fb528cef822b087dff26fc06b1f24ea0e` (`master`). Implementation branch: `feature/phase1-standalone-setup-checker-20261001`. The retained safety branch is outside this change.

## What this command does

The checker inventories prerequisites and an explicitly selected MCP configuration. By default it is offline. An explicit `--connect` permits at most one authenticated `GET /state` against one explicitly selected existing loopback session.

Three claims remain separate:

| Report | Meaning |
|---|---|
| `reported_capture` | Allowlisted **server-reported** health, backend, frame ID, dimensions and age. It is not independent capture verification. |
| `actual_frame_verification` | Always `unverified`, reason `FRAME_NOT_REQUESTED`. No frame/image endpoint is called. |
| `input_authorization` | Always `granted_by_checker: false`. Armed/dry-run/killed/foreground values are reported state, never permission to act. |

A `pass` for capture reporting means that `/state` reported healthy capture. It does not prove a valid, fresh or correctly targeted game image. An optional HWND comparison compares only the server-reported HWND. Metadata presence does not establish native importability or runtime compatibility. MCP configuration validity does not prove effective host registration or a completed handshake.

No GameLens package import occurs. The checker does not start the runtime, capture, MCP server or a game; change focus; call controls; create/repair credentials or configuration; elevate; install anything; or start recording. There is no report file or persistent storage by default. No metrics or later-phase features are included.

## Interfaces

Run from the canonical repository using an existing trusted interpreter:

```text
python -B tools/check_setup.py
  [--format text|json]
  [--scope core|desktop]
  [--mcp-host codex|claude-code --mcp-config ABSOLUTE_PATH]
  [--client NAME --clients-file ABSOLUTE_PATH | --url LOOPBACK_ORIGIN]
  [--connect]
  [--expected-target-hwnd INTEGER]
```

Defaults: `text`, `core`, offline, no MCP host or session selected. `--connect` requires exactly one session selector. Named selection requires both name and clients file. An expected HWND requires `--connect` and an integer in `1..2^64-1`. Incomplete/conflicting selectors fail before credential access or network calls. `--url` accepts only canonical `http://127.0.0.1:PORT` or `http://[::1]:PORT`, with an optional trailing slash. No credentials, other paths, query strings, fragments, DNS names, redirects, proxies or fallback clients.

Windows wrapper:

```powershell
./tools/check_setup.ps1 -Format json
./tools/check_setup.ps1 -Scope desktop -McpHost codex -McpConfig 'C:\Users\Owner\.codex\config.toml'
./tools/check_setup.ps1 -Connect -Client client-01 -ClientsFile 'B:\GameLens\clients.json'
./tools/check_setup.ps1 -Connect -Url 'http://127.0.0.1:8777' -ExpectedTargetHwnd 123456
```

Replace the example paths/HWND with the owner's actual selections. These examples were **not executed** during authoring.

The wrapper mirrors the Python options (`-McpHost`, `-McpConfig`, `-ClientsFile`, etc.) and adds `-PythonPath`. An explicit missing interpreter never falls back. Otherwise it tries the project `.venv/Scripts/python.exe`, then an existing system Python application, excluding Microsoft Store aliases. It does not use `py.exe`, installers, configuration-derived commands, elevation or execution-policy changes. It launches only this checker with `UseShellExecute=false`, quotes arguments for Windows argv, and suppresses raw child errors. Its 30-second child-process watchdog is separate from the stricter HTTP deadline; it kills only its own diagnostic child, not the GameLens session.

Python 3.11+ is required for actual checks. The **entire bootstrap file** avoids newer syntax so an older interpreter can reach its version guard before modern imports. Missing Python is handled by the PowerShell wrapper. Real older-interpreter behavior remains unverified until that optional test is run. Use a trusted Python installation; this checker is not a sandbox for a malicious interpreter or interpreter startup hooks.

## Offline environment inventory

The executing interpreter's version/architecture and project-versus-external classification are reported without disclosing its full path. Inventory is for the known Windows project environment `.venv/Lib/site-packages`, not arbitrary system search paths. A system Python fallback cannot make a missing project `.venv` pass.

Reads are limited to the known requirement files, expected project interpreter file, selected configuration files and matching `.dist-info/METADATA` files. No import of capture/imaging packages, pywin32, entry points, `.pth` files or metadata providers is used for this inventory. Direct pinned dependencies in the current requirements are recognized, including `uvicorn[standard]`, and desktop scope includes `requirements.txt` and `pywebview`. Extras/transitive dependency resolution and native loadability remain unverified. Unsupported requirement forms, unreadable/oversized metadata and duplicate distribution metadata are explicitly unverified, not silently accepted.

The directory inventory is capped at 4,096 entries. Individual configuration, handoff, requirement and metadata reads are capped at 64 KiB. Reads require named local regular files; UNC/device/network locations, symlinks/reparse points and traversal paths are refused. Files and descriptors are checked before/after reading. This is not a filesystem security sandbox against an administrator racing directory replacement. Windows drive-type queries are read-only; their actual behavior still needs Windows validation.

## Explicit MCP configuration support

No host configuration is discovered automatically.

| Host | Explicit source | Entry |
|---|---|---|
| `codex` | Selected user/project file named `config.toml` | `[mcp_servers.gamelens]` |
| `claude-code` | This canonical project's `.mcp.json` | `mcpServers.gamelens` |

The checker inspects the selected file only; it does not resolve host configuration layering, trust decisions, managed settings or cache registrations. Claude Desktop, Claude user/local `.claude.json`, plugin caches and arbitrary host commands are unsupported. Unsupported by the checker does **not** mean broken in the host.

Recognized fields: `command`, `args`, `env`; Codex additionally supports `cwd`, `enabled` and the repository-documented numeric `startup_timeout_sec`/`tool_timeout_sec` (positive finite values up to 86,400 seconds). Those timeout values are inspected only and cannot change the diagnostic deadline. Claude supports omitted `type` or `type: stdio`. Unknown settings and custom shell wrappers remain unverified. Only routing environment keys `GAMELENS_PROJECT_DIR`, `GAMELENS_CLIENTS_FILE`, `GAMELENS_URL` are recognized; other values are not printed or used.

Recognized launch forms:

1. Absolute Python file plus the canonical `plugins/gamelens/scripts/mcp_server.py` and matching `GAMELENS_PROJECT_DIR`.
2. Absolute Python file plus `-m gamelens.mcp`; module resolution remains explicitly unverified.

A present executable is not executed to prove its identity/version. Interpreter path matches against the executing/project interpreter are reported as booleans. A linked clients file is never opened unless the same file was explicitly supplied as `--clients-file`. Origins are compared against explicit selection when available. Config commands, substitutions and shell wrappers are never evaluated.

JSON duplicate keys, invalid types, over-limit reads and nesting deeper than 32 containers are rejected. TOML is parsed only from the bounded selected file and its resulting structure has the same explicit depth cap.

## Connected credential and HTTP boundaries

Only `%LOCALAPPDATA%/GameLens/agent-PORT.token` for the selected port is read. Its format must start with `GameLens-DPAPI-v1\0`; decryption uses the existing `GameLens-agent-session` entropy and lazy `win32crypt` loading. Plaintext, operator and shared-token fallbacks are not supported. If `LOCALAPPDATA` is absent, location is unverified: the checker does not call `tempfile.gettempdir()` (which can probe by writing) or guess other paths. No credential-value or token-file CLI option exists.

The decrypted credential exists transiently in process memory and the one selected request's `X-GameLens-Token` header. No copying to other sessions, printing or persistence is implemented. Python does not guarantee secure memory erasure; no such guarantee is claimed.

| HTTP boundary | Limit/behavior |
|---|---|
| Requests | At most one `GET /state`; zero if selection/credential prerequisites fail |
| Deadline | One absolute 3.000-second monotonic deadline across connect, all partial request writes and response reading; parsing also checks remaining budget |
| Headers | At most 16 KiB including terminating delimiter |
| Body | At most 128 KiB |
| Receive size | At most 4 KiB per read |
| Connection | One literal IPv4 or IPv6 loopback address; no DNS/address-family fallback |
| Retry/redirect/proxy | None |
| Framing | HTTP/1.0 or 1.1, status 200, one valid `Content-Length`, JSON content type, identity encoding |
| Unsupported framing | Duplicate headers (including identical Content-Length), transfer encoding, compression, folded/malformed headers, truncation and over-limit responses fail safely |
| Cleanup | Socket close in every exit path, including cancellation; no reader thread to abandon |

A slow trickle does not reset the deadline. Partial request writes continue at the current byte offset; they do not restart/replay the request. Receiving fewer bytes than the declared length is a safe `HTTP_PREMATURE_EOF` failure. HTTP errors expose only a numeric status and fixed reason code; no response/error text or redirect destination is printed. Content-Length/encoding restrictions are deliberate checker limits, not changes to the server.

Typical reason codes: `HTTP_DEADLINE_EXCEEDED`, `HTTP_HEADERS_TOO_LARGE`, `HTTP_BODY_TOO_LARGE`, `HTTP_FRAMING_UNSUPPORTED`, `HTTP_REDIRECT_REFUSED`, `HTTP_AUTH_REJECTED`, `STATE_JSON_INVALID`, `STATE_SCHEMA_UNSUPPORTED`, `SESSION_TOKEN_MISSING`, `SESSION_DECRYPTION_UNAVAILABLE`. Unknown internal session/probe errors are mapped to fixed fallbacks.

## Report and exit contract

Every check contains `id`, `scope`, `status`, `affects_exit`, `reason_code`, `safe_evidence`, and a fixed-template `manual_next_step`. No raw exception, arbitrary window title, log entry, action label, environment dump, secret or full user path is included. Reported scalar values are validated/allowlisted. Unknown capture backend strings become `unknown`; known `printwindow` remains identifiable.

| Status | Meaning |
|---|---|
| `pass` | This narrowly stated check was performed and satisfied |
| `warning` | Known non-blocking concern |
| `blocked` | Known prerequisite/explicit operation failed |
| `unverified` | Insufficient evidence or unsupported verification |
| `skipped` | Not requested |

| Exit | Meaning |
|---|---|
| 0 | No exit-affecting warning, blocker or unverified result; **not permission to control input** |
| 1 | Exit-affecting warning/unverified result, no blocker |
| 2 | At least one exit-affecting blocker, missing or unsupported Python |
| 64 | Invalid arguments/selectors |
| 70 | Sanitized unexpected checker/bootstrap failure |
| 130 | User cancellation handled by the Python checker |

Frame verification, native loadability, host handshake and input authorization limitations stay visible without forcing every invocation to fail. Disarmed/dry-run do not become installation failures. Non-Windows runtime compatibility remains exit-affecting unverified. Explicit expected-HWND mismatch and reported unhealthy capture are distinct blockers. The checker does not try to clear them.

## Authored validation — NOT RUN

Tests are isolated in `tests_setup/` so they do not inherit native fixtures from `tests/conftest.py`. They use temporary files, import barriers, fake DPAPI and deterministic sockets/clocks. The optional real-network case is an ephemeral loopback test server with a fake token, not a GameLens connection.

| Test module | Coverage | Authoring status |
|---|---|---|
| `test_bootstrap.py` | Missing venv/native packages, metadata, old-version gate, optional real old interpreter and Windows missing-interpreter wrapper | NOT RUN |
| `test_mcp_config.py` | Host schemas, launch forms, explicit source, no discovery/execution, routing and secret-bearing config | NOT RUN |
| `test_http_probe.py` | Partial writes, absolute deadline, stalled/trickled headers/body, duplicate length, oversized/truncated/malformed responses, no retries/proxies/DNS, socket close | NOT RUN |
| `test_session_boundary.py` | Selected port, encrypted format, missing DPAPI, secret suppression, bounded local reads, URL/constants conformance | NOT RUN |
| `test_report_contract.py` | Status/exit semantics, separate reported/verified/authorization claims, invalid arguments and sanitized failures | NOT RUN |
| `test_read_only.py` | Before/after fixture snapshots, no startup/control calls, one selected request, no persistence, bounded metadata scan | NOT RUN |
| `test_static_review_regressions.py` | PrintWindow, TOML depth, overflowing floats, safe error fallbacks and known Codex timeout fields | NOT RUN |

After separate CHUBot/Codex review and authorization, the focused command is:

```text
python -B -m unittest discover -s tests_setup -p "test_*.py" -v
```

Then perform Python/PowerShell syntax checks and `git diff --check`; these also were **not run during authoring**. An optional `GAMELENS_TEST_OLD_PYTHON` selects an already installed older interpreter for a real gate test. If absent, that case skips and is not evidence of real older-version acceptance. Windows wrapper/symlink checks may skip when unavailable; do not install dependencies, elevate or change policy just to unskip them. The existing Windows safety suite is outside this authoring run and requires its own authorization/dependency availability.

## Static-review notes and remaining limitations

Source read-through corrected missing PrintWindow classification, added TOML depth validation and non-finite JSON-number rejection, distinguished an unsuccessful connected check from an unrequested state read, and normalized unexpected internal error reasons. Regression tests were authored, not executed. The known Codex timeout fields present in the existing README are inspected without affecting transport limits.

PowerShell argument handling/process cleanup, real older-Python parsing, Windows DPAPI/drive/reparse behavior, deadline behavior under OS scheduling and compatibility with the running server's framing remain **unverified**. Filesystem reads/DPAPI are not covered by the HTTP deadline. The wrapper watchdog is not a hard real-time guarantee. A valid `/state` response is never live gameplay acceptance.

**Stop point:** saved Phase 1 code/tests/docs plus GitHub readback/diff review. No merge, deployment, local installation update, test/workflow run or next phase is authorized by this document.
