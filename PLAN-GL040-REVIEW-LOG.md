# GL-040 review log

- Host/planner/builder: Claude (Claude Code). Inspector: fresh Codex session, gpt-6-astra, effort high.
- Spec: `PLAN-GL040.md` (sha 5d2b4aa5…). Plan review skipped (`--unreviewed-spec`): the change
  is a widening of GL-039's approved surface, and the Owner asked for it directly; the
  inspection is the independent check. Limits: 2 fix rounds, 2 inspection rounds.
- Codex ran with a private CODEX_HOME (auth + model + `[windows] sandbox = "elevated"`, no MCP
  servers), as in GL-039.
- Base for the inspection: `ad8984a` — the diff also contains uncommitted GL-039.

## Build (Claude)

450 tests pass (414 before GL-040's new file; one GL-039 test updated because its premise —
F4/Delete refused for fear of Alt+F4 / Ctrl+Alt+Del — no longer holds; the sequence
allowlist tests updated to the new boundary). 11/11 mutants killed.

## Inspection 1

- **BLOCKED (operational, not a review).** Codex CLI 0.154.0. Every read failed:
  `ShellExecuteExW failed to launch setup helper: 1223` (UAC cancelled). The recreated private
  CODEX_HOME had no sandbox setup. Also flagged "code changed during inspection" — Claude wrote
  this log into the repo mid-run. Log kept outside the repo for the retry.

## Inspection 1, retry

- Sandbox state (`.sandbox*`, `cap_sid`) copied from `~/.codex`. Still 1223. Cause, from
  `~/.codex/.sandbox/sandbox.2026-09-24.log`: "sandbox setup required: sandbox users missing
  or incompatible with marker version" — the CLI update (0.153.4 → 0.154.0) invalidated the
  Windows sandbox setup for every CODEX_HOME, including the Owner's. Re-setup needs a UAC
  approval; nobody present to give it, and Claude does not approve elevation. Run stopped.
- **Status: GL-040 has had no independent inspection.** Owed once the Owner re-runs Codex's
  sandbox setup (open Codex once and approve its elevation prompt).

## Inspection 1, second retry — REVISE (fresh Codex session, gpt-6-astra high)

The Owner re-ran Codex's sandbox setup (UAC approved 12:13). Sandbox state copied again; reads
worked. Read all 22 manifest files, hashes matched. Four findings, all accepted:

- **I01 (high)** A coordinate-free press/scroll lands wherever the cursor is; in a windowed
  game a `look` can carry an unlocked cursor off the window while the game is still
  foreground. → `pointer_on_window()` checked before every ButtonDown and Scroll
  (`POINTER_OFF_TARGET`).
- **I02 (high)** A rejected release in the normal path was ignored: the sequence reported
  "sent" with the key held, and a stuck alt + a later F4 is Alt+F4. → `_release_step` raises;
  `_execute` retries `release_all` before new input; `_guard_input` refuses all new input while
  anything is unreleased (`UNRELEASED_INPUT`).
- **I03 (high)** The allowlist only governs injected names; the Owner physically holding Alt
  (or Win) makes an allowed F4/Tab a shell chord. → before every KeyDown, either Alt or Win
  held by anyone but the executor refuses; Ctrl too before escape (`FOREIGN_MODIFIER`).
  Residual (documented): a modifier pressed between the check and the press.
- **I04 (medium)** MCP: a lost /act answer was reported as "not reachable", inviting a
  duplicate. → reported as OUTCOME UNKNOWN / "may have been carried out, do not repeat", with
  the current picture; plain error only when provably not sent (no network cause, or
  connection refused).

## Fix round 1 (Claude)

464 tests (tests/conftest.py makes the real-cursor/keyboard guards deterministic; guard tests
set them explicitly). 10/10 new mutants killed. README documents the per-press checks and
residuals.

Live play on Minecraft Bedrock through the real MCP process (game foreground, SendInput):
resume click, walk+turn+jump sequence, scroll (hotbar moved), ctrl+w sprint, look down, 1.5 s
mine, E open/close, F5 ×3 (camera cycle), escape — all `sent`, all visibly effective in the
after-frames. The real `pointer_on_window` passed for a locked cursor (press and scroll).

Second live session (Owner asked first this time, after the first run took the mouse while
they were working — see memory "ask before taking the desktop"):
- **I01 guard, live:** inventory open (cursor unlocked), sequence move to (1100,760) → look
  +600 → click: `denied — the cursor is not over the target window`; a scroll right after:
  denied the same way. No click or wheel reached the desktop.
- **Drag (new `move` steps):** 2 sand dragged hotbar slot 1 → inventory row 1 slot 5. Worked.
- **Shift in a sequence:** delivered — sneak visibly lowers the camera and shows the sneak
  icon. Bedrock's shift-click did not transfer the stack (twice); that is the game's UI
  behaviour (its click-select model), not a delivery failure.
- Mining 3 s broke sand; the drops needed walking over to collect.

## Inspection 2

REVISE (fresh Codex session, gpt-6-astra high). Read all 23 manifest files, hashes matched.
None of I01–I04 re-raised. Two new findings, both accepted:

- **RV01 (medium)** `mcp.request` did not catch `http.client.IncompleteRead` (an
  `HTTPException`, not `OSError`): a truncated /act body escaped as an internal error and lost
  the unknown-outcome warning; a truncated after-image escaped `_look_safely` and lost a
  received "sent". → body reads (including HTTPError bodies) inside the handlers;
  HTTPException → ToolError with the cause kept.
- **RV02 (medium)** `bg_input_probe`'s mining test (`at_crosshair`) was not compared with the
  no-input control: a centre-only animation could certify ignored mouse input. → the control
  samples at the same offset as the input's mid-hold frame; the centre must beat the control's
  own centre churn by the margin.

Inspection budget (2) reached.

## Fix round 2 (Claude) — NOT independently inspected

468 tests pass. Regressions: truncated /act answer → OUTCOME UNKNOWN; truncated picture after
a sent action → "WAS SENT, do not repeat" kept; centre animation at interval end and a flash
only mid-interval both leave left_hold not supported (with the whole-frame control under the
margin, so the new rule is what refuses). 3/3 mutants of the fix killed.

## Status

GL-040: inspected twice; round-1 findings fixed and not re-raised; round-2 fixes (RV01, RV02)
have had no independent inspection. Not committed.
