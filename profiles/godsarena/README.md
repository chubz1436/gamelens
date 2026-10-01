# Learned Marathon profile

## Accepted delivery (October 1, 2026)

The owner accepted the existing result and closed the improvement goal.
Best clean automatic run: **867.060 seconds**, all 17 chapters completed.
The original 700-second benchmark remains **unachieved**. No scrolls were
purchased. The six-race daily limit prevented further fresh runs that day.

The current controller includes Athens portal shortcuts for 1 -> 6 and 8 -> 7,
the free Transporter return for 6 -> 1, strict arrival/NPC checks, daily-limit
recognition before Start, and mount recognition after temporary buffs expire.
Its later repaired runs are recorded below as historical evidence, not clean
speed benchmarks. No controller runs automatically after publication.

## Calibration and session history

Observed on CHUBZ, Athens, GodsArena Elysium EU, 1026 x 800, October 1, 2026.

First verified finish: **4513.097 seconds**, **1200 B-Gold**; balance 3844 -> 5044.
Target for a subsequent fresh Fitness1 run: **700 seconds**. Not yet achieved.

Manual 1 through 17 destinations:

`3, 4, 5, 3, 2, 4, 3, 1, 6, 7, 9, 10, 9, 8, 7, 6, 1 (finish)`

`gamelens.marathon.RaceProgress` contains the learned waypoints and verified
Trainer2 ->600,451 -> Trainer4 detour. Area transitions require separate
verification. Advance only after the actual exchange/completion receipt.

Run a fresh race from the calibrated Fitness1 spot (90,-129), mounted, with
GameLens already Armed/Live and the game foreground:

```powershell
./.venv/Scripts/python.exe -m gamelens.marathon_race --start --evidence-dir work/marathon/fresh-run
```

Use a NEW evidence directory for each run. This completes the learned route
and stops at the final receipt for independent review of actual game time and
reward. It does not arm, focus, change provider/model settings, purchase items,
or retry sent/uncertain actions. F12 remains the GameLens emergency stop.

If a run stops, inspect its `progress.json` and latest frame first. Resume an
already-open equation using `--resume-equation --chapter N --started-at EPOCH`;
resume a successful but unaccepted receipt using `--resume-receipt` instead.
Never repeat an exchange merely because its first image was unchanged.

Run one exchange after selecting the expected NPC:

```powershell
./.venv/Scripts/python.exe -m gamelens.marathon_control --trainer 3 --evidence-dir work/marathon/exchange-01
```

This local controller checks the selected Trainer title, handles the learned
menus, reads supported arithmetic, submits once and saves the receipt. It uses
the running GameLens guards and existing agent credential; it cannot arm, focus,
change model/provider settings or retry an uncertain action. The race runner
adds area/map checks, stable GPS arrival, calibrated NPC selection with required
portrait confirmation, and verified old/new chapter fields before progression.

The supervised math bank covers digits **0-9**, plus and equals. Other operators
remain explicit unknowns. Digit9 was learned from actual math questions; the
finish-time font was rejected as a source. Recognition handles bounded JPEG
fragments/alignment and requires agreement at two or more image thresholds.
Never fabricate a glyph or lower matching thresholds to make an unknown pass.

Validation: 60 Marathon/MCP tests passed. Twelve supervised game screenshots
read correctly (0.260s total including loading), plus untrained position-variant
fixtures. These are bounded regression checks, not a general OCR accuracy claim.
The second live race finished in3225.924s with40B-Gold, including learning and
repair pauses. Its last nine chapters completed without intervention. A fresh
run03 completed all17 chapters without intervention in **867.060 seconds**,
with40 B-Gold; balance5084 ->5124 independently verified. The700-second goal
remains unachieved. Route travel accounts for most of the remaining delay.

Learned portal findings (Trainer8 ->7 shortcut now integrated; fresh race
acceptance pending):

- Portal:Athens is already on slot2; stable spawn150,-150. It cannot cast
  while riding. Cast takes about6-7 seconds; Riding also has a cast bar, so
  wait for actual mounted state rather than assuming a2-second delay suffices.
- From that spawn, mounted map531,571 -> verified Trainer7 takes8.007 seconds.
  The local shortcut checks actual mounted/unmounted states, ready slot2,
  city spawn GPS settling and finished Riding cast. It refuses unknown/cooldown
  states without repeating sent actions. Race acceptance is pending.
- Portal:Marathon was reversibly bound to empty slot3. Stable spawn15,-27.
  Its map exit606,536 reached222,2; clicking the visible portal did not cross.
  Do not use this unproven route in a timed race.
- City exit547,587 works from Trainer6's approach. It stalled at196,-138
  when attempted directly from Trainer7. Approach matters; do not assume a
  stationary GPS proves arrival at the intended destination.

Mount icons move when timed buffs expire; recognition searches the buff row.
Right-click casts the selected skill in this game and toggled Riding during
one ground-movement experiment. Never use it as a generic ground move.
The invalid foot2 ->4 timing is excluded. The closer600,451 detour's full
Trainer2-origin leg is now verified86.856s vs old102.526s (15.670s saved).

Current improvements, now being exercised in race05:

- Portal:Athens for1 ->6 and8 ->7 only when ready; otherwise use the known
  walking route. The complete1 ->portal ->6 leg measured51.271s outside a
  race, including17.898s portal/global-cooldown/remount and NPC selection.
- After actual teleport arrival wait2.3s plus the ready Riding icon before
  remounting. Observe transitional mount pixels within a bounded timeout,
  never repeat a sent portal or Riding action.
- Trainer6 ->1 uses Search/Transfer(Location), verified Transporter169,-39,
  Transmit -> Suburbs of Athens. Mounted spawn120,-200 and eventual actual
  Trainer1 verified; balances unchanged. Full race timing pending.
- Two consecutive fresh learned endpoint GPS observations can end arrival
  waits early; actual NPC portrait remains mandatory. Unknown endpoints
  retain the stationary wait and required portrait rather than guessing.
- Start-claim recognition checks the exact Manual1 digit separately from
  bounded sentence noise.16 focused Marathon tests passed after these changes.

Race04 finished1432.993s/40BGold including repair/focus pauses, balance5164
verified. Race05 originalstart1790831430.2750769 is in progress after an
accepted Manual1 was independently reviewed; its checker repair time counts.
Use its live process/progress evidence, never repeat Start or an exchange.

Latest verified update: race05 finished **1235.095s**,40B-Gold, balance5204.
It includes both the claim-check repair and a checkpoint serialization repair.
The latter is fixed by normalizing portal flags to Python bool; regression
coverage added. **68 combined Marathon/MCP tests passed in30.96s**.
In-race8 ->7 portal completed in28.281s including exchange;6 ->Transporter ->1
completed in35.140s including exchange. Neither is a whole-race700s claim.

Rejected exploration: Marathon Transporter only offers Athens. Its physical
Suburbs entrance leads to-200,63, not close to Trainer4. The tall portal beam
is not its ground base; left-click ground movement and actual circle crossing
were verified. No race route uses the Marathon portal experiment.

The B-Gold Shop sells Athens Portal Scroll and Athens Suburb Scroll for7B-Gold
each (Goods tab, row2 columns2/3). Instant warp is the tooltip description;
actual spawn/mounted/cooldown behavior remains untested. A proposed3 Athens +
6 Suburb batch costs63B-Gold, pending owner decision. No purchase/consumption
or scroll controller integration yet. Do not assume elapsed waiting approves
spending. Scrolls were not found among the inspected travel-looking Bag items.

Latest free test rejects Trainer7 ->515,565 ->9:64.238s navigation versus
existing63.031s. The original leg remains. Bright-background NPC Search
Location fixture accepted with a bounded whole-word mask; Event/blank rejected.
18 Marathon tests passed after that local recognition correction; GameLens
guards unchanged. Completed race folders now contain independently reviewed
verified-finish.json records, all with target_met=false.

Race06 finished1322.002s/40B-Gold, balance5244, including a GPS arrival repair.
A90,-125 frame differed only12 pixels from90,-129: the earlier loose fast
match returned before arrival and the expected-NPC guard stopped the wrong
ground click. Fast matches now require<=2pixels and consecutive unchanged GPS;
ambiguous rendering uses the stationary fallback. Held-out near-GPS regression
added. A13-pixel finish-prefix variant is accepted within20; a nonfinish
exchange remains rejected.20 Marathon tests passed after these changes.
ActualNPC Join response now rejects a seventh race:6completed, come back
tomorrow. No Start was pressed during that eligibility check. The <=700 goal
remains unachieved. Scroll purchase decision remains pending; no spending.

Daily-limit messages now produce an explicit no-Start checkpoint and zero
input when already visible; post-Join recognition also stops before Start.
XP Orb is temporary: a bare buff row is accepted as foot state only with this
character's matching HUD and a known area. Blank HUD/unexplained icon remains
unknown. Bare-foot recognition and completed remount verified live after XP
expired.22 Marathon tests passed after these readiness fixes. No new race or
scroll purchase occurred; game eligibility must reset for another fresh run.

Evidence and supervised manifests are in the calling task's visualizations
folder. The complete route, shortcuts and session continuation are also saved
in the owner's `Projects/GAMELENS/GodsArena Playbook.md` and attributed log.
