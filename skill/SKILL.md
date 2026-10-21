---
name: altium-view-sch
description: Read any Altium design without opening Altium — boards, sheets, components, pins, nets, board-to-board links — by pointing a tool at a .PrjMbd or .PrjPcb once and then querying it. Use for any hardware question about a PCB project on this machine: what a part is, what a pin connects to, where a signal goes, what is on a sheet, which supply feeds what, which board something lives on, what the whole circuit looks like. Also reads the PCB layout (.PcbDoc): placement, pads, track width/length per layer, vias, pours, stack-up, schematic-vs-layout sync, and renders annotated layout PNGs. Triggers on Altium, schematic, PCB, layout, PcbDoc, PrjMbd, PrjPcb, SchDoc, netlist, designator (IC20, U1_U_PHASE, Q1), a net name, "what's connected to", "trace this signal", "which board", "show me the circuit", and on a project name you have registered.
---

# altium-view-sch

Reads Altium's own files directly: a `.SchDoc` is an OLE compound file of
pipe-delimited records, a `.MbsDoc` is a zip of JSON, a `.PrjPcb` is INI. No
Altium process, no MCP, about a second for a whole multi-board project.

Tool: `{{TOOL}}`
(call it by absolute path; it finds everything else from its own location).
Written as `altium_view.py` below. On Windows `python` may be `py`.

## Point it at a project first

Nothing is preconfigured. Register a project once from its path — the boards,
their sheets and each board's net-naming mode are then discovered from the
project files:

```bash
python altium_view.py open "C:/Users/Public/Documents/Altium/Thing/Thing.PrjMbd"
```

Works on a `.PrjMbd` (multi-board: member boards found automatically), a
`.PrjPcb` (single board), or a folder containing either. Don't know the path:

```bash
python altium_view.py scan "C:/Users/Public/Documents/Altium"
python altium_view.py projects          # what is already registered
```

Registered projects stay registered. Every later command uses the last one
unless you pass `--project <slug>`.

## Then ask it things

| Question | Command |
|---|---|
| What is this design? | `altium_view.py summary` |
| What is IC20, and what are its pins on? | `altium_view.py part IC20` |
| What is on net FLASH_CS? | `altium_view.py net FLASH_CS` |
| Every net a part touches | `altium_view.py nets IC20` |
| What is on a sheet? | `altium_view.py block ISOCAN` |
| What does a board-to-board connector carry? | `altium_view.py link C_9` |
| Where is anything (part, net, MPN)? | `altium_view.py find ISO1042` |
| **Where does this pin's signal go?** | `altium_view.py trace U_PHASE.6` |
| **Every connection in the design** | `altium_view.py netlist` |
| **Every part's value, package and MPN** (one line per sheet) | `altium_view.py netlist [Board] --collapse --parts` |
| What should a human look at? | `altium_view.py issues` |
| Do two boards' connectors really mate pin to signal? | `altium_view.py pcb mate [C_10]` |
| Where are the drawings? | `altium_view.py drawings` |

`trace` walks the net and hops every board-to-board connector it reaches, so
one command crosses from an MCU pin to a gate driver on another board.

`netlist` is the one to reach for when the question is about the circuit as a
whole rather than one part. It prints every net as `NET: PART.pins ...` and
costs about 6,400 tokens for this whole three-board design -- less than a
handful of `part` queries, and lossless. `--collapse` writes a repeated channel
once as `<CH>` with the instances listed (about 4,700 tokens here) and only
when the channels are genuinely identical. `--parts` appends every component's
value, package (passives) and MPN, grouped per sheet and collapsed across
channels -- the values a circuit review needs. Never read `system_model.json`
instead, not even with a script: the same connectivity costs about 14,000
tokens there, the whole file about 66,000, and everything it holds is reachable
through a command.

## Reviewing a whole design: do this, in this order

1. `altium_view.py issues` -- open pins (with No-ERC state), MbsDoc sync (each
   one already checked by pad position, see `pcb mate`), and
   **value_mpn_mismatch**: parts whose schematic value and purchasing MPN name
   different parts (e.g. a 12 V module whose MPN is the 5 V one). Treat every
   mismatch as a finding until the designer says which side is right.
2. `altium_view.py netlist --collapse --parts` -- the whole circuit with values,
   one read. Review per sheet from this, not from repeated `part` calls.
3. `altium_view.py pcb mate` -- every board-to-board connector paired by the
   physical position of its pins on both layouts (pin numbers are not trusted:
   SMD vs THT headers and row- vs column-wise numbering differ while mating
   correctly). A `!! SIGNAL MISMATCH` here is a real wiring error.
4. `altium_view.py pcb check` -- schematic vs layout, if layout questions follow.
5. Device behaviour (thresholds, pin functions, ratings) still needs the
   datasheet: fetch it and cite it; the model only knows the symbol.

A designator is unique **within a board**, not across the design. Every command
answers for each board the name exists on and says so. To mean one board, write
it as `Board/Name` -- `part Control/C1`, `net Power/DC+`, `trace Driver/U1.5`
-- or put the board name second (`part C1 Control`). Designators are never
rewritten: `C1` is what Altium, the BOM and the silkscreen call it.

**Never answer a hardware question from memory of an earlier turn.** Run a
query — it re-validates itself against the design files before answering.

## The PCB layout, too

The same tool reads each board's `.PcbDoc` (found from its `.PrjPcb`), parses
it in well under a second, and caches the parse until the file changes. Use it
instead of the Altium MCP for anything read-only about the layout.

| Question | Command |
|---|---|
| Boards, size, layer count, in sync with the schematic? | `altium_view.py pcb summary` |
| Pad-by-pad schematic vs layout (ECO pending, parts parked off-board, swapped nets) | `altium_view.py pcb check [Board]` |
| Stack-up (copper weight, dielectric thickness, er) | `altium_view.py pcb stack Driver` |
| Where is a part, its pads and their nets, what is next to it | `altium_view.py pcb part Power/Q1_U_PHASE` |
| A net's copper: length and widths per layer, vias, pour area, pads | `altium_view.py pcb net Driver/G_HS_U_PHASE` |
| Parts within N mm (edge to edge, with side) | `altium_view.py pcb near Power/Q1_U_PHASE 5` |
| Distance between parts or pads (`DES.PAD`) | `altium_view.py pcb dist Power/D3_U_PHASE Power/Q1_U_PHASE.1` |
| Every cap/resistor across two of an IC's nets: copper path length + vias from each pad to the IC pin, verdict | `altium_view.py pcb caps Driver/U1_U_PHASE` |
| Length along the actual copper between two pads on one net | `altium_view.py pcb path Driver/C4_U_PHASE.1 Driver/U1_U_PHASE.16` |
| **Routed length pad to pad, as Altium measures it** (tracks + arcs along the path, stubs excluded; `with via barrels` adds the via height from the stack-up) | `altium_view.py pcb length IC1.112 R50.2` |
| **Every signal between two parts, through series resistors, length-matched** (DDR/SDRAM/LTDC groups) | `altium_view.py pcb xsignals IC1 U1 --ref FMC_SDCLK --tol 10 --group data=FMC_D*,FMC_NBL* [--z]` |
| **Design rules, net classes and xSignals as set in the layout** (what length/width/clearance rule actually applies; whether a net split by a series resistor is joined into one xSignal) | `altium_view.py pcb rules [length]` |
| Length matching the way the designer set it up: only the leg after the series resistor | add `--after-series` to `pcb xsignals` |
| Same-layer copper gaps between different nets (spacing / creepage review) | `altium_view.py pcb clearance Power --max 3 --rows 200` |
| Board-to-board connectors paired by pin position on both layouts | `altium_view.py pcb mate [C_10]` |
| **Look at the layout, with marks** | `altium_view.py pcb render Driver --around U1_U_PHASE:20 --layers L4 --nets G_HS_U_PHASE,S_HS_U_PHASE --parts U1_U_PHASE --measure A B --mark x,y,text --labels [--bottom] -o out.png` |

`render` writes a PNG (default under the project cache, `pcb/`) -- open it with
the Read tool to actually look. Highlighted nets get bright colours and
everything else is dimmed; `--layers` takes `L1..Ln`, `top`, `bottom` or the
layer's own name; render one copper layer at a time when nets overlap. `--bottom`
mirrors to a view from below.

Layers are reported as `L1..Ln` from the top of the physical stack, with
Altium's own layer name beside it. Coordinates are mm in Altium's absolute
frame. A PCB designator follows the schematic model (`D3_U_PHASE`), built from
the component's source designator and its channel room.

`pcb net` also checks real copper connectivity: per layer it merges every
piece of the net's copper (pours with cut-outs removed, tracks, arcs, pads,
vias) on a 0.05 mm grid, so separately drawn pours that overlap or touch count
as one island; vias and through-hole pads join layers. It then says whether
every pad of the net is joined, and if not, which pads sit on which piece.

Caveats: saved files only; pours are read as last repoured in Altium (an
un-repoured polygon shows its old copper); gaps under ~0.1 mm can read as
joined; a pour's net comes from its polygon definition.

## Read the `!!` lines before the answer

Every command prints its caveats on **stdout, above the answer**, each starting
`!!`. They are part of the answer, not decoration:

- `CHECKED against the PcbDoc pad nets ...` -- the exported netlist is stale or
  missing, but every pad net in the saved PcbDoc (Altium's own compile, pushed
  by the last ECO) agrees with the offline netlist. Connectivity of placed parts
  is confirmed; say "checked against the layout" rather than "verified by Altium".
- `PCB DIFFERS from the schematic on <board>` -- the layout and schematic
  disagree (ECO pending, or a real difference): run `pcb check <board>`.
- `UNVERIFIED: <board> ...` -- no exported netlist and no layout that agrees.
  The connectivity is derived by this tool and nothing independent has
  confirmed it. Say so when you use it.
- `DISAGREES with Altium's own netlist` -- stop and run `validate`.
- `STALE: the model does not match the design files` -- a rebuild was needed
  and failed. The answer describes an older schematic.
- `X exists on N boards` / `N sheets match` -- the answer covers all of them;
  a designator is only unique within one board.
- `X is used by N different parts` -- one designator, two physical parts. Only
  one is described and nothing keyed by designator can separate them.

If a caveat is printed, carry it into what you tell the user. Never restate an
`UNVERIFIED` answer as a checked fact.

## An open pin is not automatically a mistake

A red No-ERC cross in the schematic is the designer saying "this pin is meant
to be left open", and the tool reads those crosses. `part` marks such pins
**NC**, and `issues` separates the three cases:

- `intentional_no_connect` (info) -- crossed, so deliberate.
- `unmarked_open_pin` (warning) -- open and not crossed, while the *same pin on
  an identical part* is crossed. That difference between two copies is the one
  worth a look.
- `open_pin` (info) -- open, uncrossed, and nothing to compare against. Unused
  MCU I/O lives here; so does a genuine omission.

So never call a pin "unconnected -- possible error" without checking whether it
carries a cross.

## Compile masks are honoured

Anything lying wholly inside a compile mask (the grey box with a triangle in
Altium) is left out exactly as Altium compiles it: not in the netlist, the PCB
comparison or the parts list. `issues` lists such parts as `compile_masked`, so
an alternative part parked under a mask is never reported as a duplicate
designator or a miswired connector. Check that list before calling a masked
part's absence an error.

## Freshness is automatic; know the one hole

The model is a **cache of the Altium files, not a second source of truth**.
Every query re-checks the design files (a few milliseconds) and rebuilds
(about a second) if anything moved — including a sheet that was *added*.
Concurrent sessions are safe: a lock serialises rebuilds and every output is
written atomically. `altium_view.py freshness` reports the state without
repairing it; `altium_view.py rebuild` forces one.

**The hole: unsaved edits in Altium.** Only files on disk are visible. If the
user says they *just changed* something, ask whether it is saved before
trusting the answer. `ALTIUM_VIEW_NO_REFRESH=1` disables the auto-rebuild for
when the design drive is not mounted.

## What a human can open

`altium_view.py drawings` prints the paths. Per project:

- `equivalent_circuit.html` — the whole design as **one circuit**, board
  boundaries dissolved, each PCB tinted, every board-to-board connector
  labelled, cross-board nets highlighted. Open this when someone asks to see
  the circuit rather than a sheet.
- `system_diagram.html` — board-level block diagram.
- `schematics/index.html` — the sheets as drawn, with the design's own symbols;
  hover a wire to trace its net.
- `system_model.md` — the whole model as text, usually about 10 KB.
- `raw/provenance.json` — every source file with size, mtime and SHA-256. This
  is how a person checks what the agent actually read.

## Limits worth stating out loud

- The model describes **the design**, not the devices in it. Part numbers,
  values and pin *names* come from Altium symbol metadata, which can be wrong.
  Any claim about what a device *does* — a pin's function, a rating, a
  threshold, a register — needs the manufacturer datasheet, fetched and cited.
  `part <des>` prints the datasheet link when the library carries one.
- A designator used twice collapses in anything keyed by designator, including
  Altium's own netlist. `issues` lists them, and `part` warns inline. A part
  placed as several sections (a quad op-amp) is *not* this: it is merged into
  one component, and `part` says how many sections.
- `kind` (ic / passive / connector / power_module) and `domain` (power / ground
  / signal / local) are this tool's own heuristics from names and pin counts,
  not facts from the design. `likely_sources` on a rail is a guess too.
- `trace` follows electrical nets and stops at a component: a buffer's or an
  isolator's output is a different net. Trace again from the output pin.
- The netlister is checked against netlists Altium itself exported, where those
  exist (`altium_view.py validate`). On a newly registered project there are
  none, so the netlist is unverified — say so rather than implying it was
  checked. `EXTRACT.md` covers exporting them via the Altium MCP.

## If something is missing

`README.md` next to the tool explains the pipeline and every file in it.
