# drivers

Local scripts that drive the demo world through `bot.py`. They are not part of
the `gamelens` package and nothing in the harness imports them; each one exists
because playing the game taught something that is easier to keep as code than as
a note.

| script | what it does | what it knows |
|---|---|---|
| `approach.py` | turn to a bearing and walk until something stops you | collision, not colour: every appearance rule for "is that a tree" was refuted by the weather |
| `chop.py` | walk to a trunk and take logs from its base | aim at the **base** block — a log broken at eye height drops inside the tree's own footprint, behind the columns still standing — then step into the gap, because drops only come to you from about a block away |
| `craft_kit.py` | logs → planks → crafting table → sticks, in the player's 2x2 | the panel is found in the frame and the slots computed from Minecraft's own 176x166 texture geometry, never from remembered pixels |
| `tools.py` | place the table, craft a wooden pickaxe and hoe on the 3x3 | right-click places **one** item in a slot; left-click places the whole stack. `/act` could not do this until the button parameter existed |
| `survive.py` | respawn, dig in, and prove the hole is sealed | four deaths in one night, every one while standing still in the open. "Sealed" means dark from all four sides, checked, not assumed |
| `fight.py` | swing at what is in front and break off before dying | reads the heart row off the HUD. The reader is crude and over-reports during the damage flash — see the note in the file |

Run them from the repo root with the venv's Python, with GameLens armed and
live:

```bash
.venv/Scripts/python.exe drivers/chop.py -25 4
```
