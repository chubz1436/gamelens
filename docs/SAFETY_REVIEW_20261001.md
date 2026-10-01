# Safety review implementation

Base: 6e208a00fefef4c87a1ff80bb4e3343a25ee9cbe.
Status: authored through GitHub; no local execution or test results yet.

## Changes
1. Runtime shutdown kills/releases input before recorder finalization.
2. Shared literal-loopback, no-proxy, no-redirect MCP transport.
3. Additive sequence progress fields and partial-input warning. Existing terminal
   outcome names are preserved for compatibility. A failed native send may still
   have an uncertain effect; confirmed-send counters are not a game-effect claim.
4. Installer detects stale standalone registration and requires explicit
   -RepairRegistration. Unknown environments/non-stdio transports require manual
   review; known named-client configuration is preserved.
5. Bounded input queue with immediate refusal and explicit arbiter rejection.
6. Align credential/session documentation with owner-authorized session controls.
7. Add isolated Windows CI (pull requests/manual dispatch), not real game control.

## Validation handoff
Use GPT-6.1 Sol only. Start in the canonical GameLens project folder and preserve
unrelated local changes. Inspect this feature branch in an isolated worktree.
Run compile checks and tests/test_review_safety.py,
tests/test_transport_security.py, tests/test_installer_registration.py,
tests/test_mcp.py, tests/test_multi_client.py, tests/test_http_auth.py,
tests/test_plugins.py, tests/test_session_control_routing.py, then the relevant
existing safety/outcome/sequence/capture regression suite. Report failures,
skips, and unrun tests separately.

The installer test uses a fake Codex command and temporary directory; never run
the real installer as an unannounced test. The new safety tests mock native
injection. No credential/configuration changes or real game input are needed.

Manual acceptance, if separately authorized: use a disposable receiver, verify
Stop during a recorder delay, partial sequence reporting and queue overflow,
then optionally validate actual game capture. Never claim CI proves WGC/RDP,
driver stability, full game play, or delivery of every input.

Do not merge or publish a release from an untested branch. The owner requested
notification when authoring finishes before the Codex testing phase begins.
