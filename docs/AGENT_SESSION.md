# Owner-authorized agent session control

Owner policy, October 1, 2026: authenticated agents may Arm and Go live for an
instructed game task, including an instruction sent to the agent remotely.
The agent does not need the operator credential or a second owner click.

`gamelens_session` accepts only `status`, `arm`, `live`, or `stop`. In the installed
ten-client provider, include the exact configured `client` on each call. There is
no default client or broadcast. Discover names with `gamelens_clients` first.

```json
{"client":"client-01","action":"status"}
{"client":"client-01","action":"arm"}
{"client":"client-01","action":"live"}
```

Inspect state and a fresh image before enabling and acting: intended target,
geometry, healthy capture, and foreground. Arm and Live send no game input.
Startup is still disarmed/dry-run. State can be Armed/Live while input remains
blocked by focus, capture, kill, watchdog or observation guards. Recording and
race Start are separate requested actions.

`stop` uses the emergency latch and releases held input. Arm/Live cannot clear
that latch. Restart after an emergency stop only when the owner's subsequent
instruction explicitly resumes that session. A dropped response is uncertain:
read status and decide, never blindly replay a mutating call.

If the server is closed and running it belongs to the owner's request, inspect
windows with `.venv/Scripts/python.exe -m gamelens --list`, then start the desktop
app against the verified exact HWND and selected port:

```powershell
.venv/Scripts/pythonw.exe GameLens.pyw --target VERIFIED_HWND --port 8777
```

Wait for the authenticated session and inspect its target, then Arm/Live. Existing
desktop sessions attach without restarting capture. Each client keeps its own
session credential. Do not launch all ten clients implicitly.

Remote owner instructions are handled by the local agent. This change keeps the
HTTP API on loopback, with session authentication and Host/Origin checks; it does
not publish a remote endpoint. For a VM, the same agent/session controls must be
installed and verified in the guest. Foreground input is still shared within
each desktop; this feature does not implement background controls.
