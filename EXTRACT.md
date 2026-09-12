# Extraction runbook (agent + Altium MCP)

> **This is no longer the normal path.** `python extract_offline.py` reads the
> Altium files straight off disk in a few seconds, needs neither Altium nor the
> MCP, and is checked node-for-node against Altium's own netlists by
> `validate_offline.py`. Use that; see `README.md`.
>
> This runbook is kept for two reasons. It is how the **golden netlists** in
> `raw/<Board>.net` were produced, and those are what prove the offline reader
> correct — regenerate them after a big schematic change so the check stays
> honest. And it is the fallback if the offline reader ever meets a construct it
> does not handle (a bus, a multi-level hierarchy, a variant).
>
> The gotchas at the bottom are worth reading either way: every one of them
> fails *silently*.

The MCP pipeline has three stages. Only stage 1 needs Altium:

| Stage | Who runs it | Output |
|---|---|---|
| 1. Extract | an agent, via the Altium MCP, with Altium open | `raw/` |
| 2. Build | `python build_system_model.py` | `system_model.json`, `system_model.md` |
| 3. Render | `python render_system.py` | `system_diagram.html` |

Stages 2 and 3 are pure file processing: deterministic, re-runnable offline,
and diffable. Re-run them alone whenever you change the model or the drawing.

---

## Stage 1, step by step

### 1. Find the projects

```
proj_list_open
```

Note the `.PrjMbd` and every `.PrjPcb` under it.

### 2. Copy the multi-board schematic into `raw/`

The `.MbsDoc` is a **zip of JSON** and is parsed directly from disk -- no MCP
call needed, and no Altium needed either.

```bash
cp "<...>/TTR_QUACKDRIVE_V2_SYSTEM.MbsDoc" raw/
```

`logical.json` inside it carries the whole inter-board model: modules, their
connectors, and every pin-to-pin link with the net name on each side.

### 3. For each sub-project, in turn

**a. Focus the project.** This is the step people get wrong. The `project_name`
and `scope` arguments on the `proj_*` tools are **ignored** -- the focused
project follows the *active document*. Passing a project name returns data for
whatever is focused, silently and with no error.

```
app_list_documents                    # find a SchDoc of this project, loaded:true
app_set_active_document  <that SchDoc>
proj_get_focused                      # confirm before continuing
proj_load_sheets                      # loads the rest of the sheets
```

If no document of the project is loaded, `proj_open` the project first; note
that `proj_open` returns `focused:false` and does not by itself switch focus.

**b. Export the netlist** (lands on disk, costs no context):

```
proj_export_netlist  output_path=<abs>/raw/<Board>.net
```

**c. Get the designator list:**

```bash
python extract_helper.py designators raw/<Board>.net
```

**d. Pull the components.** Pass that array to:

```
proj_get_component_info_many  designators=[...]
```

For a board of any real size the result exceeds the tool-output limit and the
harness **writes it to a file** instead, printing the path. That is the
intended path, not a failure -- it keeps hundreds of components out of the
conversation. File it:

```bash
python extract_helper.py collect <printed-path> <Board>
```

Each component comes back with `sheet` and a full pin-to-net map, which is why
this one call replaces a separate netlist parse, BOM and sheet lookup.

**e. Optional, for part numbers and values:**

```
proj_export_bom_html  output_path=<abs>/raw/<Board>_bom.html
```

### 4. Check and build

```bash
python extract_helper.py status
python build_system_model.py
python render_system.py
```

`build_system_model.py` re-parses the netlist independently and compares it
against the component dump. Trust the `cross-check: ok` line; investigate
anything else before believing the diagram.

---

## Gotchas, and why they matter

**`project_name` / `scope` are ignored on `proj_*` and `obj_query`.**
Focus follows the active document. Always confirm with `proj_get_focused`.

**`design_review_snapshot` reads the active *PCB*, not the focused project.**
With a multi-board workspace open it will happily return a different board's
components and report success. Do not use it for this pipeline.

**The netlist `format` argument is ignored.** Every value produces PADS
format. Do not describe the output as Orcad or EDIF.

**Pin designators can contain spaces, commas and `...`.**
A symbol that merges several pads onto one logical pin names the pin after the
pad list -- the MGJ1D121505VMC DC/DC on the driver board has pins literally
called `1, 2`, `15, 16` and `11 ... 14`. Splitting netlist node lines on
whitespace shreds these into phantom nodes, and reading `11 ... 14` as a pin
*range* invents pins 12 and 13 that do not exist. `build_system_model.py` cuts
node boundaries at known designators instead.

**Channel designators are `<logical>_<room>`.**
Repeated sheets (the three phase channels here) flatten to `U1_W_PHASE` and so
on. The builder recovers the room names by finding the suffix set that both
covers every designator on the sheet and leaves an identical prefix set in each
group, so a coincidental shared ending such as `_PHASE` is not mistaken for a
room.

**Two parts can share a designator.** `proj_get_component_info_many` returns
one entry per designator, so an un-annotated `R?` pair silently loses a part.
The cross-check catches this and reports it as `node_missing_from_model`.

---

## When the design changes

Re-run stage 1 for the boards that changed, then stages 2 and 3. The model and
the diagram are generated artifacts -- never hand-edit them. `raw/` is the
evidence; keep it alongside the outputs so a reviewer can trace any number on
the diagram back to something Altium said.
