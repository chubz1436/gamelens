# GL-040 — Agent controls for any game, not only Minecraft

Owner: "gawin mong maging compatible ang controls sa kahit anung laro hindi lang naman para
sa minecraft ang gamelens". Builds on GL-039 (same branch, uncommitted; see PLAN-GL039.md).
Build authorized; not committed.

## Goal

An agent can play games other than Minecraft through the same `/act` / `gamelens_act`
surface, with every GL-039 safety rule intact.

## What changed

1. **Keys** (`gamelens/input.py` `KEY_NAMES`): F1–F11, numpad 0–9 and operators, backspace,
   right shift/ctrl/alt, insert/delete/home/end/pageup/pagedown, punctuation by name and by
   character. Still refused: Windows/menu keys, F12 and Pause (kill switch), PrintScreen,
   lock keys.
2. **Sequence keys** (`arbiter.SEQUENCE_KEYS`): now `KEY_NAMES − {alt, ralt, escape, esc}`
   (was letters/digits/space/shift/ctrl/arrows). Tab, enter and F-keys become sequence keys.
   Argument: every shell chord (Alt+Tab, Alt+F4, Ctrl+Esc, Ctrl+Shift+Esc) needs alt or
   escape; the Windows keys are not nameable; the executor runs one action at a time, so a
   single-key `alt` cannot overlap a sequence. Known unclosed: shift ×5 (Sticky Keys prompt),
   ctrl+shift layout switch where configured.
3. **Mouse**: `Button.X1/X2` (mouse4/5) with mouseData; `button_from_name` shared by click,
   press and sequences. `Scroll` step (vertical/horizontal, ±10 notches, whole, non-zero),
   kind `scroll` and sequence step `scroll`. Release of X buttons carries mouseData.
4. **Pointing in sequences**: steps `move {x,y}` and `click {button, x?, y?}`. Parsed to
   `PointAt` (image pixels); `sequence_action` maps each through `_map_point` — the same
   bounds + geometry checks `click_action` uses (now shared). One bad point refuses the whole
   sequence before anything runs. Points count as new inputs (rate limiter, interlocks).
5. **Rebind with points**: `_bind(points=[...])`; every point's 33x33 patch must be unchanged;
   the worst point is reported; `submit_sequence` passes `sequence_points(steps)`.
6. MCP schema/README updated. `tools/bg_input_probe.py` labelled Minecraft-Java-only (its
   cases and in-world detector are that game's) — not generalised on purpose.

## Acceptance

- `pytest -q` passes (450). New `tests/test_any_game.py`.
- Mutants on each new rule killed (11/11): points not passed on rebind, ralt/esc allowed in
  sequences, F12 nameable, unsigned negative scroll, X release with wrong mouseData, only the
  first point checked, PointAt not counted as input, PointAt not mapped, scroll unclamped,
  `horizontal` not type-checked.

## Inspector focus

Whether widening the sequence allowlist opens a chord Windows (not the game) acts on; whether
any pointer path skips bounds/geometry/freshness/patch checks; whether any new press path can
leave something held (X buttons, clicks in sequences) or bypass release_all; HTTP 400-before-
queue for every new malformed field.
