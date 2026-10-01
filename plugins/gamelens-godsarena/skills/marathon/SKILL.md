---
name: marathon
description: Run or resume an owner-authorized GodsArena Marathon with GameLens using the saved seventeen-chapter route, learned portal and Transporter shortcuts, receipt checks and continuation evidence. Use for Fitness Trainers, Health Manuals, race timing and Marathon route improvement.
---

# GodsArena Marathon with GameLens

Use the GameLens MCP from the core GameLens plugin. This companion does not create a second MCP connection.

1. Read `gamelens_profile` with `profile:"godsarena-marathon"` once for the recorded route and calibration. If tools are unavailable, read [the compact route](references/route.md) and the local project profile. Reuse known knowledge rather than rereading all manuals.
2. Inspect current `gamelens_state` and `gamelens_see`. Existing progress/receipt takes precedence over an assumed new race. Check current area, mounted state, geometry, trainer and chapter; preserve the original start time when resuming. Do not restart the app/runtime or repeat Start to recover an interrupted run.
3. Before a fresh authorized run, confirm mounted Fitness1 and daily eligibility. If the NPC says six races already completed/come back tomorrow, stop before Start. No reset time was established. Installing this plugin is not authorization to race now.
4. Use the local learned runner for an explicitly authorized full race only after live checks. It requires a new evidence directory and Armed/Live session; an authenticated agent may enable that session with `gamelens_session` arm then live under the owner's game instruction. No implicit scheduled/autonomous race, forced focus, paid API, purchase or repeated uncertain action.
5. Advance only after actual old/new Manual receipt and expected NPC portrait. Evaluate math only when observed and supported. Observe unknown/cooldown/transitional states; do not invent a digit or repeat a sent cast.
6. On finish, review actual time and reward receipt and record balances/results. Include all repair pauses. Best clean recorded run867.060s was accepted by the owner; original700s target was not achieved. Do not re-open that closed goal unless the owner requests it.

When the owner requests Marathon recording, use `gamelens_recording` action `status` and then `start` once (default30fps) before sending race Start; retain an existing active recording instead of starting twice. Stop recording after the actual finish or interruption and inspect its returned result. A failed start/stop requires status inspection before another decision, not a blind retry. This records the selected game window, video only/no audio, and changes no Arm/Live/focus or game input state.

Source lives in the companion GameLens project: `profiles/godsarena/marathon-guide.json`, `gamelens/marathon_race.py`; durable continuation is in the existing `Projects/GAMELENS/GodsArena Playbook.md` and task log. Saved state is historical; fresh live observations establish current state.
