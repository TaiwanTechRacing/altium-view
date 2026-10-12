# altium-view

Read an Altium design without opening Altium — boards, sheets, components,
pins, nets and board-to-board links, straight from the `.PrjMbd` / `.PrjPcb` /
`.SchDoc` files on disk. Built so an agent can answer a hardware question in one
command instead of hunting through schematics, and so a person can see exactly
what the agent read.

> **Humans: paste this to your coding agent, then stop reading.**
>
> ```
> Clone this repo, run `python install.py`, then register my Altium project
> with it and give me a summary of the design.
> ```

Everything below is written for that agent.

---

## Install

```bash
pip install olefile          # the only dependency
python install.py            # installs the altium-view-sch skill for this user
python install.py --project  # or only for the repo you are standing in
python install.py --check    # what is installed; changes nothing
```

`install.py` writes this checkout's absolute path into the skill, so the tool
works from any directory afterwards. Re-run it if the repo moves.

## Use

```bash
python altium_view.py open "C:/.../Thing.PrjMbd"   # register a design, build it
python altium_view.py summary                      # the design in ~30 lines
python altium_view.py netlist                      # every connection, compact
python altium_view.py trace IC20.51                # follow a signal across boards
```

| Question | Command |
|---|---|
| What projects are on this machine? | `scan <folder>`, `projects` |
| What is this design? | `summary` |
| What is IC20, and what is each pin on? | `part IC20` |
| What is on net FLASH_CS? | `net FLASH_CS` |
| Every net a part touches | `nets IC20` |
| What is on a sheet? | `block ISOCAN` |
| What does a board-to-board connector carry? | `link C_9` |
| Where is anything (part, net, MPN)? | `find ISO1042` |
| Where does this pin's signal go? | `trace IC20.51` |
| **The whole circuit at once** | `netlist [--collapse] [--parts]` |
| What should a human look at? | `issues` |
| Is the model current, and checked? | `freshness`, `validate` |
| Where are the drawings? | `drawings` |

Every command takes `--project <slug or path>`; without it, the last one used.
A designator is unique **within a board**, so `part C1` answers for every board
that has a C1; `part Control/C1` or `part C1 Control` picks one.

## The output contract

Anything that weakens an answer is printed on **stdout above it**, prefixed
`!!` — never only on stderr, because the reader is usually an agent capturing
stdout:

| Line | Meaning |
|---|---|
| `UNVERIFIED: <board> ...` | no Altium-exported netlist, or it predates the schematic: the connectivity is derived and unchecked |
| `DISAGREES with Altium's own netlist` | stop and run `validate` |
| `STALE: the model does not match the design files` | a rebuild was needed and failed; the answer describes an older schematic |
| `X exists on N boards` / `N sheets match` | all of them are shown |
| `X is used by N different parts` | one designator, two physical parts; only one is described |

Carry those into whatever you tell the user. Never restate an `UNVERIFIED`
answer as a checked fact.

## The whole circuit, cheaply

`netlist` prints every connection as `NET: PART.pins ...`, pins grouped under
their part, nothing repeated:

```
MCAN_TX: IC8.2 IC20.99
+18V_HS_<CH>: C13_<CH>.1 C14_<CH>.1 PS1_<CH>."15, 16" U1_<CH>.16  [x3: U, V, W]
NetC5_1: C5.1 C6.1 CH1.3 (auto)
```

Measured on a three-board design, the same connectivity costs about **29,000
tokens** as pretty-printed JSON, **14,000** from the packed `system_model.json`,
**2,200** as a PADS netlist and **1,600** in this form — about **6,400** for all
three boards, or **4,700** with `--collapse`. It round-trips back to the model
exactly, so the saving is in the encoding, not in what it leaves out.

Notation: `*` marks a No-ERC pin, `(auto)` marks a net the designer never named,
a pin designator containing a comma is quoted (`PS1."15, 16"`), and
`--collapse` writes a repeated channel once — but only after checking that the
channels really are identical.

## Intent, not just connectivity

Altium's No-ERC cross is the one record that separates a pin left open on
purpose from one left open by accident, so it is read too. `part` prints **NC**
beside those pins, the rendered sheets draw the cross, and `issues` splits open
pins into deliberate (`intentional_no_connect`), suspicious
(`unmarked_open_pin` — the same pin is crossed on an identical part) and merely
unexplained (`open_pin`). A cross sitting on nothing is reported, because it
marks nothing.

## Is the netlist right?

It is checked, not assumed. Put a netlist Altium itself exported at
`<cache>/raw/<board key>.net` and run `validate`: it diffs node by node, refuses
to claim success when it checked nothing, and reports `STALE REFERENCE` (exit 3)
when the export is older than the schematic. On the design this was built
against:

| Board | Nets | Nodes | Result |
|---|---:|---:|---|
| Control Board | 192 / 192 | 715 / 715 | match |
| Driver Board | 108 / 108 | 546 / 546 | match |
| Power Board | 97 / 97 | 399 / 399 | match |

`EXTRACT.md` covers exporting those through the Altium MCP. A newly registered
project has none, so its netlist is unverified until you add them — say so
rather than implying it was checked.

## The model is a cache, not a snapshot

Checking validity costs about 4 ms, rebuilding about 1.3 s, so every query
checks first and rebuilds when the files moved — including when a sheet was
*added*. Concurrent sessions are safe: rebuilds are serialised by a lock file
and every output is written atomically. A rebuild that fails halfway is caught
as well, by comparing the model's own timestamp against the extraction record.

The one thing no check can see is an **unsaved edit still open in Altium**.

## Where things live

Code stays in the checkout; per-project data goes to `~/.altium-view/` so design
folders, which are usually git repositories, do not collect generated files.

```
~/.altium-view/
  index.json                  registered projects
  <slug>/spec.json            discovered boards and sheets
  <slug>/raw/                 extracted evidence + provenance.json (every
                              source file with size, mtime and SHA-256)
  <slug>/system_model.json    the model, packed (~66k tokens; use `netlist`)
  <slug>/system_model.md      the model as text (~3k tokens)
  <slug>/equivalent_circuit.html, system_diagram.html, schematics/
```

`ALTIUM_VIEW_HOME` moves that root, `ALTIUM_VIEW_PROJECT` sets the default
project, `ALTIUM_VIEW_NO_REFRESH=1` disables the automatic rebuild.

## Why it can read these files at all

| File | Actually is |
|---|---|
| `.SchDoc` | OLE compound file; the `FileHeader` stream is length-prefixed, pipe-delimited ASCII records |
| `.MbsDoc` | zip of JSON — the multi-board logical model, pin for pin |
| `.PrjPcb` / `.PrjMbd` | INI |

Connectivity is geometric: pin ends, wires, junctions, net labels, power
objects, ports and sheet entries are unioned by coincidence, named by Altium's
own precedence, then hierarchy and repeated channels are expanded.

Two format traps, both silent: a coordinate has a companion `X1_Frac` field in
hundred-thousandths, so matching coordinate keys loosely turns 70540 into a
coordinate and destroys every bounding box; and `PartCount` is stored as
**sections + 1**, so a plain resistor records 2 and a quad op-amp 5 — read as a
section count it makes one part look like four colliding designators.

## Files

| | |
|---|---|
| `altium_view.py` | the CLI — everything goes through this |
| `install.py` | installs the skill with this checkout's path |
| `skill/SKILL.md` | what the agent reads |
| `discover.py` | a path to boards, sheets, hierarchy mode |
| `project.py` | which project is selected; cache layout; the registry |
| `altium_sch.py` | `.SchDoc` reader — records, components, pins, symbol graphics |
| `netlist_offline.py` | geometric netlister, hierarchy and channel expansion |
| `extract_offline.py` | writes `raw/` and `raw/provenance.json` |
| `validate_offline.py` | diffs the offline netlist against Altium's |
| `build_system_model.py` | `raw/` to `system_model.json` + `.md` |
| `model_io.py` | packs and unpacks the model file |
| `render_system.py` | board-level block diagram |
| `render_schematic.py` | the sheets as drawn, with the design's own symbols |
| `render_equivalent.py` | the whole design as one circuit |
| `query.py` | the read commands behind `altium_view.py` |
| `freshness.py` | cache validity, locking, atomic writes |
| `naming.py` | short names derived by comparison, not hard-coded |
| `boards.py` | the selected project's boards |
| `EXTRACT.md` | the Altium-MCP path, for golden netlists and as a fallback |

## One standing caveat

The model describes **the design**, not the devices in it. Everything about a
part — pin function, rating, polarity, timing — comes from Altium symbol
metadata, which is a hint and is sometimes wrong; `kind` and `domain` are this
tool's own heuristics. Check the manufacturer datasheet before relying on any
of it.

## PCB layout (`altium_pcb.py`, `pcb_query.py`)

`altium_view.py pcb ...` reads each board's `.PcbDoc` directly (OLE storages:
text records `Board6`/`Components6`/`Nets6`/`Polygons6`, binary records
`Pads6`/`Tracks6`/`Arcs6`/`Vias6`/`Fills6`/`Regions6`/`ShapeBasedRegions6`,
layouts per KiCad's Altium importer). The parse is cached in
`<cache>/pcb/<PcbDoc stem>.json`, keyed by the file's size and mtime.

- `altium_pcb.py` -- the parser; `read_pcb(path)` returns mm, Altium frame.
- `pcb_query.py` -- `summary`, `check` (pad nets vs the schematic model, by
  connectivity), `stack`, `part`, `net`, `near`, `dist`, `render` (matplotlib
  PNG with highlighted nets, boxed parts, measurements and marks).

Verified by `pcb check` on the quackDrive Control board: every placed pad sits
on the net the schematic gives it.
