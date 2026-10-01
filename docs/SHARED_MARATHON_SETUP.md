# Running the saved GodsArena Marathon on another PC

The repository includes the learned 17-exchange route, arithmetic glyph reader,
portal shortcuts and guarded controller. Best verified clean run is **867.060
seconds (14 minutes 27 seconds)**. A 700-second run has not been proved. Results
on another PC or character require live verification; this is saved calibration,
not an automatic guarantee of the same finish time.

## Install on Windows

Use Windows with Python 3.11 and Microsoft Edge WebView2 Runtime. From the cloned
repository, create the environment and install the app dependencies:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-desktop.txt
powershell -NoProfile -ExecutionPolicy Bypass -File tools/install_desktop.ps1
```

For Codex plugin/MCP use, install with `tools/install_plugins.ps1` and reload the
MCP connection. `tools/install_multi_client.ps1` optionally creates ten named
local slots. Installers bind paths to this checkout; rerun them after moving it.
They start no game, race, recording or live input.

## Match the profile before starting

- The reference game image is **1026 × 800**. Font, UI scale and game layout must
  match the saved templates. Select the actual GodsArena client in GameLens.
- Learned Riding must be ready in hotbar **7** and Portal Athens in **2**.
  Arrival, cooldown and actual mounted state are checked separately.
- Start mounted at Fitness Trainer 1 in Athens' Suburb and verify eligibility.
  The observed daily cap is six races per character; the reset time is unverified.
- `profiles/godsarena` is the original reference profile; the separate
  `profiles/godsarena/characters/atong69` adapts that character's HUD. A different
  character or UI may need supervised template calibration. Do not rename a
  profile to imply it was verified for another character or weaken unknown-state
  checks to make it run.
- Check healthy capture, target identity and foreground. The local operator or
  an owner-authorized authenticated agent may Arm then Go live. See
  [agent session controls](AGENT_SESSION.md).

The tested shortcuts use the learned skill and free Transporter. No portal
scroll purchase is required by this route. Portal/math overlap is explicit
opt-in, works only when the portal is ready, and otherwise uses normal exchange
and navigation. Both 1→6 and 8→7 overlaps have live evidence; verify the actual
receipt before advancing and remount only after the destination is confirmed.

## Start one explicitly requested race

After the matching profile and live session are verified, a single new race
without video recording can be run from the checkout:

```powershell
.venv/Scripts/python.exe -m gamelens.marathon_race --start --profile profiles/godsarena --evidence-dir evidence/new-race-01 --overlap-portals
```

Use a **new** evidence directory and the actually verified profile. For a
matching Atong69 session, substitute its separate profile directory. For a
different character, calibrate first. This example contacts local port8777; do
not assume a multi-client selector changes this CLI's destination.

The runner can stop on an unfamiliar menu, math rendering, missing focus or
changed capture. Trainer10's selected-NPC HUD Menu can open a context menu;
confirm the actual race dialogue before any exchange. A stopped run is still
unfinished, and its original game timer continues. Do not start again or replay
a math submission to hide a delay. Final success requires visual review of the
actual finish time/reward receipt, not just a successful process exit.

Input still shares this Windows desktop's mouse/keyboard and requires the game
in the foreground. Window capture can continue behind another window; separate
background input is pending. A VM needs GameLens installed in the guest and its
capture/session/input behavior tested there.
