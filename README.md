# CS-105 Roster Sync for JMRI

Put every locomotive in your JMRI roster onto a TCS CS-105 command station, with names and function labels, in one step. Pick up a TCS throttle afterwards and every locomotive is there by name, with a bell button that rings the bell.

## Why you might need it

DecoderPro can already send one roster entry to the CS-105 (File > Export > "Roster entry to TCS CS-105"). Two things get in the way:

- **It only works for trains the CS-105 already has.** If you have never selected that address on a throttle, DecoderPro refuses, and there is no way to tell it to create the train.
- **The CS-105 forgets a train without a name.** A train that only got labels, or was only selected on a throttle, comes back blank, or not at all, after the command station restarts.

This script does the whole roster at once. It asks the CS-105 to create each missing train, gives every new train a name so it survives a restart, and writes the same labels DecoderPro would.

## What you need

- JMRI 5.x with an LCC (OpenLCB) connection that reaches the CS-105, for example a TCS LCC buffer on USB.
- The CS-105 powered and on that LCC bus.
- Your locomotives in the JMRI roster with DCC addresses. Function labels and the comment field are used when present.

Tested with JMRI 5.16 and one CS-105. If you try it with another firmware, an issue saying whether it worked is welcome.

### Other LCC command stations

Nothing in the train side is TCS-only: train node IDs, the Train Search that creates a train, and the settings keys come from OpenLCB and OpenMRN. The script finds the command station by asking who produces event `09.00.99.FE.FF.FF.00.01`, so a command station from another maker that produces it, with the same train settings, should work too. Try **Show plan** first. It writes nothing, and the log names the command station it found and how. Settings the command station does not have are listed as "settings not found" rather than written. Please open an issue saying whether it worked.

## Use it

1. Download [`cs105_roster_sync.py`](cs105_roster_sync.py) and keep it somewhere handy, for example in your JMRI user files folder.
2. In PanelPro or DecoderPro choose **Scripting > Run Script...** and pick the file.
3. A window opens and shows the plan. Nothing is written yet.
4. Read the plan, and press **Apply...** if it looks right. You will be asked once more before anything is written.
5. Press **Show plan** again afterwards. Every train should now be an UPDATE with nothing left to create.

The plan has one line per roster entry:

| Word | Meaning |
|---|---|
| CREATE | The CS-105 has no train for this address. It will be created and named. |
| UPDATE | The CS-105 has the train. Its labels (and description, if the roster has a comment) will be refreshed. |
| SKIP | The roster entry has no DCC address. |
| KEEP | A train on the CS-105 with no roster entry. It is listed only and never changed. |

### Choices in the window

- **Only roster IDs:** a comma-separated list such as `GP9 101, SW1 7`. Leave it empty for the whole roster. An ID that is not in the roster is flagged, so a typo cannot silently do nothing.
- **Train names:** the roster ID, or the road name and number from the roster (for example "JF&N 7"). Entries with no road data fall back to the ID.
- **Rename trains that already exist:** names are normally set only on trains the script creates. Tick this to rename existing trains too.
- **LCC connection:** shown only when JMRI has more than one.

## What it changes and what it does not

It writes only the train's settings on the CS-105: the name, the description (from the roster comment, only when the comment is not empty) and, for F1 to F28, the label, icon and whether the button is momentary. A label JMRI knows as an icon, such as Bell, Horn or Dimmer, becomes that icon. Anything else is sent as text.

It does not move trains, change track power, touch turnouts or read or write decoder CVs. Consist settings are left as they are. Trains on the CS-105 that are not in your roster are left alone.

### Safety checks

- **No command station, no writing.** Before planning, the script asks every node on the LCC bus to announce itself and asks who produces the command-station event `09.00.99.FE.FF.FF.00.01`. If nothing answers, it looks for a node whose model name contains CS-105, for firmware that does not produce the event. If neither finds one, Apply stays off. A powered-down command station otherwise looks like an empty one, and the plan would offer to create every locomotive.
- **Exactly one command station.** A new train is created by a request the whole bus hears, so with two command stations on one connection both could create it. The script lists them and keeps Apply off.
- **One run at a time.** A second Apply while one is still running is refused.
- **More than one LCC connection:** you choose which one. The script does not guess.

## Restarting the CS-105

Deleted trains stay on the LCC bus until the command station restarts, and a restart is also the test that names were stored. Power-cycle the CS-105 when nothing is moving, then press **Show plan** again.

## Running it from your own scripts

Set any of these before the script runs, for example by putting the lines at the top of a copy of the file. The window does not open, and the log ends up in `RESULT`.

```python
CS105_APPLY = True          # default False: plan only
CS105_ONLY = "GP9 101,SW1 7"
CS105_RENAME = True
CS105_NAMES = "road"        # or "id" (default)
CS105_CONNECTION = "M"      # system prefix or name, when JMRI has more than one LCC connection
```

The settings are cleared when the script starts, so a later run from the Scripting menu cannot inherit `CS105_APPLY = True`. A refused or failed apply raises `RuntimeError`.

Under plain Python 3 the file plans from a copy of a roster folder, with no device:

```
python3 cs105_roster_sync.py path/to/folder-with-roster.xml --values
```

## How it works

- The command station is the node that answers Identify Producer for `09.00.99.FE.FF.FF.00.01`. TCS defined this event; JMRI already uses the matching `…00.02` to find a programming track. Boosters produce `…00.03`.
- Each DCC address maps to a train node: short address *n* is `06.01.00.00.00.nn`, and long address *n* is `06.01.00.00.(C0+high).low`. JMRI's LCC throttle uses the same mapping.
- A missing train is created with the OpenLCB Train Search Protocol: an Identify Producer for event `09.00.99.FF`, followed by the address digits and the flags Allocate, Exact, Address-only, DCC and, for long addresses, Force-long. The CS-105 answers by creating the train.
- Settings are written through the train's configuration memory, using the keys DecoderPro's export uses (`Train.Name`, `Train.User Description`, `Train.Functions(n).Display`, `.Momentary`, `.Description`). `Functions(n)` is F(n+1), and F0 is the headlight.
- The label-to-icon table is copied from JMRI 5.16 `TcsExportAction`, so both tools agree. It also accepts "Shunt" and "Blowdown", the spellings the command station firmware's own table uses for the same codes.

Sources: the OpenLCB Train Search Protocol Standard (2024-07-22); JMRI 5.16 `jmri.jmrit.symbolicprog.TcsUploadAction` and `TcsExportAction`; the JMRI help page "OpenLCB on TCS Hardware".

## Tests

```
python3 -m unittest discover -s tests
```

The tests cover the address mapping, the Train Search event, the plan and the settings, using a sample roster in `tests/sample_roster`. The parts that talk to JMRI and the CS-105 are checked by hand on a layout.

## License

GPL-2.0, the same as JMRI, from which the icon table is copied. See [LICENSE](LICENSE).
