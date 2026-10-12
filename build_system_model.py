#!/usr/bin/env python3
"""
Build a single machine-readable system model from the raw Altium extraction.

Inputs  (raw/):
  TTR_QUACKDRIVE_V2_SYSTEM.MbsDoc   multi-board schematic (zip of JSON)
  <Board>_components.json           proj_get_component_info_many dump (pins carry nets)
  <Board>.net                       PADS netlist (cross-check only)

Outputs:
  system_model.json                 the model agents read
  system_model.md                   the same model as compact text

Run:  python build_system_model.py
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from freshness import atomic_write_text  # noqa: E402

import model_io
import project                                          # noqa: E402
from boards import BOARDS as BOARD_TABLE                  # noqa: E402
from project import P                                     # noqa: E402

# Classification is driven by lib_ref first and the designator prefix second.
# Doing it the other way round mislabels parts: 'U_PHASE' is a 12-way header,
# not a 'U' device, and the bulk caps on the power board carry a manufacturer
# part number as their lib_ref rather than 'C'.
CONNECTOR_LIBREF_RE = re.compile(
    r"header|block_terminal|^43045-|^7461\d{3}$", re.IGNORECASE
)
TESTPOINT_LIBREFS = {"TEST TP", "DEBUG"}
MECHANICAL_LIBREFS = {"M4", "MOUNTING HOLE"}
PASSIVE_PREFIXES = {"R", "C", "CE", "L", "F", "D", "LED", "PTVS", "FL", "CH"}
CONNECTOR_PREFIXES = {"J", "JP", "JTAG", "CTRL", "HS", "LS", "CURRENT", "DC", "PHASE"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def split_designator(des: str) -> tuple[str, str | None]:
    """Split 'U1_W_PHASE' into ('U1', 'W_PHASE') once rooms are known."""
    return des, None  # replaced per-board by infer_rooms()


def infer_rooms(designators: list[str]) -> list[str]:
    """
    Recover Altium channel room names from flattened designators.

    Altium's default channel format is '$Component_$RoomName', so a repeated
    sheet yields the same logical designators once per room. A suffix set is
    accepted as the room set only when EVERY designator ends with exactly one
    of the suffixes and every room carries the identical set of prefixes --
    that rules out coincidental shared endings such as '_PHASE' alone or the
    '_IN1' on a non-repeated sheet.
    """
    if len(designators) < 2:
        return []
    max_parts = max(d.count("_") for d in designators)
    for depth in range(1, max_parts + 1):
        groups: dict[str, set[str]] = defaultdict(set)
        ok = True
        for d in designators:
            parts = d.split("_")
            if len(parts) <= depth:
                ok = False
                break
            suffix = "_".join(parts[-depth:])
            prefix = "_".join(parts[:-depth])
            if not prefix:
                ok = False
                break
            groups[suffix].add(prefix)
        if not ok or len(groups) < 2:
            continue
        prefix_sets = list(groups.values())
        if all(s == prefix_sets[0] for s in prefix_sets):
            return sorted(groups)
    return []


def designator_prefix(designator: str) -> str:
    """Leading letters of a designator, e.g. 'IC20' -> 'IC', 'U_PHASE' -> 'U'."""
    m = re.match(r"^([A-Za-z]+)", designator)
    return (m.group(1) if m else "").upper()


def classify_part(designator: str, lib_ref: str, comment: str, n_pins: int) -> str:
    """Coarse kind used for grouping and for picking what to draw."""
    lib_ref = lib_ref or ""
    comment = comment or ""
    prefix = designator_prefix(designator)

    if CONNECTOR_LIBREF_RE.search(lib_ref) or CONNECTOR_LIBREF_RE.search(comment):
        return "connector"
    if lib_ref in TESTPOINT_LIBREFS:
        return "testpoint"
    if lib_ref in MECHANICAL_LIBREFS:
        return "mechanical"
    if prefix in ("MH", "M", "H"):
        return "mechanical"
    if prefix == "TP":
        return "testpoint"
    if prefix in CONNECTOR_PREFIXES:
        return "connector"
    if prefix == "PS":
        return "power_module"
    if prefix == "Q":
        return "switch"
    if prefix in PASSIVE_PREFIXES:
        return "passive"
    if n_pins >= 3:
        return "ic"
    return "passive"


POWER_PREFIXES = (
    "VDD", "VCC", "VSS", "AVDD", "AVCC", "A5V", "VREF", "AVREF",
    "AVBIAS", "VDDA", "VDDIO", "VRAW", "DC+", "DC-",
)


def net_domain(name: str) -> str:
    """Classify a net by name so the diagram can colour power vs signal."""
    n = (name or "").upper()
    if n in ("GND", "AGND", "DGND", "PGND") or n.endswith("_GND") or n.startswith("GND"):
        return "ground"
    if re.match(r"^[+-]?\d", n) or n.startswith(POWER_PREFIXES):
        return "power"
    if n.startswith("NET"):
        return "local"
    return "signal"


# --------------------------------------------------------------------------
# multi-board schematic
# --------------------------------------------------------------------------

def parse_mbs(mbs_path: Path) -> dict:
    """Read the .MbsDoc (a zip of JSON) into modules + pin-level links."""
    with zipfile.ZipFile(mbs_path) as z:
        logical = json.loads(z.read("logical.json").decode("utf-8"))

    pin_index: dict[int, dict] = {}
    entry_index: dict[int, dict] = {}
    modules = []

    for m in logical["modules"]:
        src = m.get("source", {})
        mod = {
            "id": m["id"],
            "designator": m.get("designator"),
            "title": m.get("title"),
            "project_name": src.get("projectName"),
            "project_path": src.get("sourceProject"),
            "board_file": src.get("boardFileName"),
            "connectors": [],
        }
        pin_to_entry = {
            e["key"]: e["value"] for e in m.get("entryPinMap", {}).get("map", [])
        }
        for e in m.get("entries", []):
            entry_index[e["id"]] = {
                "module_id": m["id"],
                "module_title": mod["title"],
                "designator": e.get("calculatedDesignator"),
            }
        for c in m.get("components", []):
            conn = {
                "designator": c.get("designator"),
                "comment": c.get("comment"),
                "pins": [],
            }
            for p in c.get("pins", []):
                params = {q["name"]: q.get("value") for q in p.get("parameters", [])}
                rec = {
                    "pin": p.get("number") or p.get("pinId"),
                    "net": params.get("ExternalNetName"),
                    "module_id": m["id"],
                    "module_title": mod["title"],
                    "connector": c.get("designator"),
                    "entry_id": pin_to_entry.get(p["id"]),
                }
                pin_index[p["id"]] = rec
                conn["pins"].append({"pin": rec["pin"], "net": rec["net"]})
            mod["connectors"].append(conn)
        modules.append(mod)

    links = []
    for c in logical.get("connections", []):
        ec = c.get("entriesConnection", {})
        e1 = entry_index.get(ec.get("entry1"), {})
        e2 = entry_index.get(ec.get("entry2"), {})
        wires = []
        for pc in ec.get("physicalConnections", []):
            pp = pc.get("pinPair", {})
            a = pin_index.get(pp.get("pin1"), {})
            b = pin_index.get(pp.get("pin2"), {})
            wires.append({
                "pin": pc.get("designator"),
                "a_pin": a.get("pin"), "a_net": a.get("net"),
                "b_pin": b.get("pin"), "b_net": b.get("net"),
            })
        links.append({
            "designator": c.get("designator"),
            "a_board": e1.get("module_title"),
            "a_connector": e1.get("designator"),
            "b_board": e2.get("module_title"),
            "b_connector": e2.get("designator"),
            "wire_count": len(wires),
            "wires": wires,
        })

    return {
        "modules": modules,
        "links": links,
        "conflicts": logical.get("conflicts", []),
    }


# --------------------------------------------------------------------------
# per-board model
# --------------------------------------------------------------------------

def norm_node(designator: str, pin: str) -> str:
    """Node key with all whitespace squeezed out of the pin designator."""
    return f"{designator}.{re.sub(r'\\s+', '', str(pin))}"


def parse_netlist(path: Path, known: set[str]) -> dict[str, list[str]]:
    """
    Parse the PADS netlist as an independent cross-check of the component dump.

    A node is NOT simply a whitespace-delimited token. Symbols that merge
    several pads onto one logical pin carry the pad list inside the pin
    designator itself -- the MGJ1D121505VMC DC/DC has pins literally named
    '1, 2', '15, 16' and '11 ... 14' -- so those node strings contain spaces
    and would otherwise be shredded into phantom nodes. Nodes are therefore
    cut at the known designators instead, and everything up to the next one is
    the pin designator.
    """
    nets: dict[str, list[str]] = {}
    cur = None
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("*SIGNAL*"):
            cur = line.split(None, 1)[1].strip()
            nets[cur] = []
            continue
        if line.startswith("*"):
            cur = None
            continue
        if cur is None:
            continue
        node_des: str | None = None
        node_pin: list[str] = []
        for tok in line.split():
            head = tok.split(".", 1)
            if len(head) == 2 and head[0] in known:
                if node_des is not None:
                    nets[cur].append(norm_node(node_des, " ".join(node_pin)))
                node_des, node_pin = head[0], [head[1]]
            elif node_des is not None:
                node_pin.append(tok)
        if node_des is not None:
            nets[cur].append(norm_node(node_des, " ".join(node_pin)))
    return nets


def build_board(key: str, title: str) -> dict:
    comp_file = P.raw / f"{key}_components.json"
    # Altium's own export is the cross-check when it exists; otherwise fall
    # back to the offline netlist so the comparison still catches a regression.
    # A golden netlist exported before the last schematic edit is not a
    # reference at all -- every edit since shows up as a fault in the model,
    # which is the opposite of what the check is for -- so it is set aside and
    # said so rather than used.
    golden = P.raw / f"{key}.net"
    net_file = golden if golden.exists() else P.raw / f"{key}_offline.net"
    reference = "altium" if golden.exists() else "offline"
    stale_golden = None
    if golden.exists():
        from boards import sheet_paths
        newest = max((p.stat().st_mtime for p in sheet_paths(key)), default=0)
        if newest > golden.stat().st_mtime:
            stale_golden = datetime.fromtimestamp(
                golden.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            net_file = P.raw / f"{key}_offline.net"
            reference = "offline (Altium's export predates the schematic)"
    data = load_json(comp_file)
    comps_raw = data["components"]
    net_meta = data.get("net_meta") or {}

    # --- rooms (channel instances) per sheet -----------------------------
    by_sheet: dict[str, list[str]] = defaultdict(list)
    for c in comps_raw:
        by_sheet[c["sheet"]].append(c["designator"])
    sheet_rooms = {s: infer_rooms(ds) for s, ds in by_sheet.items()}

    def split(des: str, sheet: str) -> tuple[str, str | None]:
        for room in sheet_rooms.get(sheet, []):
            if des.endswith("_" + room):
                return des[: -(len(room) + 1)], room
        return des, None

    # --- components ------------------------------------------------------
    # Components are keyed by designator because that is how everything else
    # refers to them, but a design can carry the same designator twice (an
    # un-annotated 'R?'). Those collisions are counted and reported rather than
    # silently overwritten, and the true part count is kept separately.
    # A multi-section part (a quad op-amp's A/B/C/D) is placed as several
    # records sharing one designator. That is one physical component, not a
    # designator collision, and merging the sections is the difference between
    # "U1 has 3 pins" and "U1 has 14 pins" -- the first answer is simply wrong.
    # Sections declare more than one section and never repeat a pin; two
    # different parts that happen to share a designator do repeat them.
    by_designator: dict[str, list[dict]] = defaultdict(list)
    for c in comps_raw:
        by_designator[c["designator"]].append(c)

    def sectional(group: list[dict]) -> bool:
        if len(group) < 2 or not all(g.get("sections", 1) > 1 for g in group):
            return False
        seen: set[str] = set()
        for g in group:
            pins = {str(p.get("pin")) for p in (g.get("pins") or [])}
            if pins & seen:
                return False
            seen |= pins
        return True

    merged_raw: list[dict] = []
    duplicates: dict[str, int] = {}
    for des, group in by_designator.items():
        if len(group) > 1 and sectional(group):
            head = dict(group[0])
            head["pins"] = [p for g in group for p in (g.get("pins") or [])]
            head["section_count"] = len(group)
            head["sheets"] = sorted({g["sheet"] for g in group})
            merged_raw.append(head)
            continue
        if len(group) > 1:
            duplicates[des] = len(group)
        merged_raw.extend(group)
    comps_raw = merged_raw

    components = {}
    nets: dict[str, list[dict]] = defaultdict(list)
    for c in comps_raw:
        des = c["designator"]
        sheet = c["sheet"]
        logical, room = split(des, sheet)
        params = c.get("parameters", {}) or {}
        pins = c.get("pins", []) or []
        kind = classify_part(des, c.get("lib_ref", ""), c.get("comment", ""), len(pins))
        components[des] = {
            "designator": des,
            "logical_designator": logical,
            "room": room,
            "sheet": sheet,
            "comment": c.get("comment"),
            "lib_ref": c.get("lib_ref"),
            "footprint": c.get("footprint"),
            "kind": kind,
            "pin_count": len(pins),
            "mpn": params.get("Manufacturer_Part_Number") or None,
            "manufacturer": params.get("Manufacturer_Name") or None,
            "datasheet": params.get("Datasheet Link") or None,
            "sections": c.get("section_count", 1),
            "sheets": c.get("sheets") or [sheet],
            # set when two different parts share this designator: every answer
            # keyed by it describes only one of them
            "ambiguous": duplicates.get(des, 0),
            "no_connect_pins": sorted(p.get("pin") for p in pins if p.get("no_erc")),
        }
        for p in pins:
            net = p.get("net")
            if not net:
                continue
            node = {
                "designator": des,
                "pin": p.get("pin"),
                "pin_name": p.get("name"),
                "sheet": sheet,
                "room": room,
            }
            if p.get("no_erc"):
                node["no_erc"] = True
            nets[net].append(node)

    # --- blocks = schematic sheets --------------------------------------
    blocks = []
    for sheet in sorted(by_sheet):
        members = [d for d in by_sheet[sheet]]
        rooms = sheet_rooms.get(sheet, [])
        key_parts = [
            {
                "designator": components[d]["logical_designator"] if rooms else d,
                "comment": components[d]["comment"],
                "lib_ref": components[d]["lib_ref"],
                "kind": components[d]["kind"],
                "mpn": components[d]["mpn"],
            }
            for d in sorted(members)
            if components[d]["kind"] in ("ic", "power_module", "switch")
        ]
        # collapse channel duplicates
        seen = set()
        uniq = []
        for kp in key_parts:
            sig = (kp["designator"], kp["comment"])
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(kp)
        connectors = sorted({
            components[d]["logical_designator"] if rooms else d
            for d in members if components[d]["kind"] == "connector"
        })
        blocks.append({
            "sheet": sheet,
            "name": sheet.replace(".SchDoc", ""),
            "component_count": len(members),
            "rooms": rooms,
            "key_parts": uniq,
            "connectors": connectors,
        })

    # --- nets that cross sheets = the block interconnect -----------------
    block_links: dict[tuple[str, str], list[str]] = defaultdict(list)
    net_records = {}
    for name, nodes in nets.items():
        sheets = sorted({n["sheet"] for n in nodes})
        domain = net_domain(name)
        net_records[name] = {
            "name": name,
            "domain": domain,
            # how Altium arrived at this name: a power port, a net label, a
            # sheet port, or nothing at all -- "auto" means the designer never
            # named this net, so the name carries no intent
            "named_by": (net_meta.get(name) or {}).get("named_by") or "auto",
            "node_count": len(nodes),
            "sheets": sheets,
            "nodes": nodes,
        }
        if len(sheets) > 1 and domain == "signal":
            for i in range(len(sheets)):
                for j in range(i + 1, len(sheets)):
                    block_links[(sheets[i], sheets[j])].append(name)

    # --- power rails: where each supply is generated and consumed ---------
    power_rails = []
    for name, rec in net_records.items():
        if rec["domain"] not in ("power", "ground"):
            continue
        sources = sorted({
            n["designator"] for n in rec["nodes"]
            if components[n["designator"]]["kind"] in ("power_module", "ic")
            and re.search(r"VOUT|OUT|VDD|VCC", str(n["pin_name"] or ""), re.I)
        })
        power_rails.append({
            "net": name,
            "domain": rec["domain"],
            "node_count": rec["node_count"],
            "sheets": rec["sheets"],
            "likely_sources": sources,
        })
    power_rails.sort(key=lambda r: (-r["node_count"], r["net"]))

    # --- cross-check against the netlist ---------------------------------
    check = {"netlist_present": net_file.exists(), "reference": reference,
             "stale_golden_exported_utc": stale_golden}
    missing_from_model: list[str] = []
    if net_file.exists():
        nl = parse_netlist(net_file, set(components))
        # nodes on a duplicated designator cannot be compared: 'R?.1' names two
        # different physical pins, so neither side can key on it
        def ambiguous(node: str) -> bool:
            return node.split(".")[0] in duplicates
        nl_nodes = {(n, t) for n, toks in nl.items() for t in toks
                    if not ambiguous(t)}
        our_nodes = {
            (name, norm_node(n["designator"], n["pin"]))
            for name, rec in net_records.items() for n in rec["nodes"]
            if not ambiguous(norm_node(n["designator"], n["pin"]))
        }
        missing_from_model = sorted(f"{a}:{b}" for a, b in (nl_nodes - our_nodes))
        check.update({
            "netlist_nets": len(nl),
            "model_nets": len(net_records),
            "netlist_nodes": len(nl_nodes),
            "model_nodes": len(our_nodes),
            "only_in_netlist": missing_from_model[:20],
            "only_in_model": sorted(f"{a}:{b}" for a, b in (our_nodes - nl_nodes))[:20],
        })

    # --- issues worth a human's eye --------------------------------------
    issues = []
    for des, n in sorted(duplicates.items()):
        issues.append({
            "severity": "error",
            "kind": "duplicate_designator",
            "where": f"{des} x{n}",
            "detail": f"{n} components share the designator {des!r}. Altium's own "
                      f"netlist and any per-designator query collapse them into "
                      f"one, so they are invisible to most tools -- annotate the "
                      f"schematic to separate them.",
        })
    # An identifier that touches nothing is a fact about the schematic the
    # reader cannot see from a netlist: a net label on empty canvas names no
    # net, and a No-ERC cross dragged off its pin marks nothing -- so whatever
    # it was meant to mark is still unexplained.
    prov_file = P.raw / "provenance.json"
    if prov_file.exists():
        try:
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prov = {}
        loose: dict[str, list[str]] = defaultdict(list)
        for entry in prov.get("unattached_identifiers", []):
            if len(entry) < 5 or entry[0] != key:
                continue
            board_key, sheet_name, _room, kind, text = entry[:5]
            where = entry[5] if len(entry) > 5 else ""
            loose[kind].append(f"{text or where} on {sheet_name}")
        for kind, items in sorted(loose.items()):
            what = ("No-ERC cross" if kind == "no_erc" else kind.replace("_", " "))
            issues.append({
                "severity": "warning",
                "kind": "unattached_" + kind,
                "where": f"{len(items)} {what}(s)",
                "detail": ("marks nothing -- it sits on no pin, wire or terminal, "
                           "so the pin it was meant to mark is still unmarked: "
                           if kind == "no_erc" else
                           "attached to nothing, so it names no net: ")
                          + ", ".join(items[:8]),
            })

    if stale_golden:
        issues.append({
            "severity": "info",
            "kind": "golden_netlist_stale",
            "where": golden.name,
            "detail": f"Altium's exported netlist ({stale_golden}) is older than "
                      f"the schematic, so it was set aside and the offline "
                      f"netlist was used instead -- meaning nothing independent "
                      f"checked this board. Re-export it (see EXTRACT.md).",
        })
    if missing_from_model:
        issues.append({
            "severity": "error",
            "kind": "node_missing_from_model",
            "where": f"{len(missing_from_model)} node(s)",
            "detail": "present in the netlist but absent from the component dump -- "
                      "usually a designator shared by two parts, so only one was "
                      "returned: " + ", ".join(missing_from_model[:6]),
        })
    for des, c in components.items():
        if "?" in des:
            issues.append({
                "severity": "error",
                "kind": "unannotated_designator",
                "where": f"{c['sheet']} / {des}",
                "detail": f"designator still contains '?' (value {c['comment']!r}) -- "
                          f"it will not map to a PCB footprint",
            })
    # --- open pins: which ones the designer said were meant to be open ---
    marked: list[str] = []
    bare: list[tuple[str, str]] = []
    for name, rec in net_records.items():
        if rec["node_count"] != 1:
            continue
        node = rec["nodes"][0]
        where = f"{node['designator']}.{node['pin']}"
        if node.get("no_erc"):
            marked.append(where)
        elif rec["domain"] != "local":
            # a lone pin on a *named* net is still worth a warning: the name
            # says it was meant to go somewhere
            issues.append({
                "severity": "warning",
                "kind": "single_node_net",
                "where": f"net {name}",
                "detail": f"only one pin on this net ({where}) and no No-ERC "
                          f"cross marking it deliberate",
            })
        else:
            bare.append((node["designator"], str(node["pin"])))

    if marked:
        parts = sorted({d.split(".")[0] for d in marked})
        issues.append({
            "severity": "info",
            "kind": "intentional_no_connect",
            "where": f"{len(marked)} pin(s) on {len(parts)} part(s)",
            "detail": "left open on purpose -- a No-ERC cross is placed on each "
                      "of these pins in the schematic: " + ", ".join(marked[:24])
                      + (" ..." if len(marked) > 24 else ""),
        })

    # An open pin is only ambiguous in isolation. Where the same part appears
    # twice and one copy has the pin crossed, the other copy's bare pin is the
    # interesting one -- it is the difference between the two that is unlikely
    # to be deliberate.
    marked_set = {tuple(d.split(".", 1)) for d in marked}
    type_of = {d: (c.get("lib_ref"), c.get("comment")) for d, c in components.items()}
    marked_by_type: dict[tuple, set[str]] = defaultdict(set)
    for des, pin in marked_set:
        marked_by_type[type_of.get(des)].add(pin)
    odd = [f"{des}.{pin}" for des, pin in bare
           if pin in marked_by_type.get(type_of.get(des), set())]
    if odd:
        issues.append({
            "severity": "warning",
            "kind": "unmarked_open_pin",
            "where": f"{len(odd)} pin(s)",
            "detail": "open and not crossed, while the same pin is crossed on "
                      "another part of the same type -- either a missing No-ERC "
                      "cross or a missing connection: " + ", ".join(sorted(odd)[:16]),
        })
    if bare:
        issues.append({
            "severity": "info",
            "kind": "open_pin",
            "where": f"{len(bare)} pin(s)",
            "detail": "on nothing but their own auto-named net and carrying no "
                      "No-ERC cross, so the schematic does not say whether that "
                      "is deliberate (unused MCU I/O usually is): "
                      + ", ".join(f"{d}.{p}" for d, p in sorted(bare)[:12])
                      + (" ..." if len(bare) > 12 else ""),
        })

    return {
        "key": key,
        "title": title,
        "sheet_count": len(by_sheet),
        "component_count": len(comps_raw),
        "unique_designators": len(components),
        "duplicate_designators": duplicates,
        "net_count": len(net_records),
        "blocks": blocks,
        "components": components,
        "nets": net_records,
        "block_links": [
            {"a": a, "b": b, "net_count": len(v), "nets": sorted(v)}
            for (a, b), v in sorted(block_links.items())
        ],
        "power_rails": power_rails,
        "cross_check": check,
        "issues": issues,
    }


# --------------------------------------------------------------------------
# assembly + reporting
# --------------------------------------------------------------------------

def refresh_link_nets(links: list[dict], by_title: dict) -> None:
    """
    Put each board's own schematic net on both ends of every inter-board wire.

    The multi-board document stores a net name per connector pin, but only as
    of the last time it was synchronised with the child projects. A pin swapped
    on one board since then still carries its old name there, so the link view
    and every cross-board trace would describe a wiring that no longer exists
    -- and hide exactly the mismatch the swap created. The cached name is kept
    beside the live one so the gap can be reported.
    """
    index: dict[str, dict] = {}
    for title, b in by_title.items():
        index[title] = {
            (nd["designator"], str(nd["pin"])): name
            for name, rec in b["nets"].items() for nd in rec["nodes"]
        }
    for lk in links:
        stale = []
        for w in lk["wires"]:
            for side in ("a", "b"):
                pins = index.get(lk[f"{side}_board"])
                if pins is None or w[f"{side}_pin"] is None:
                    continue
                live = pins.get((lk[f"{side}_connector"], str(w[f"{side}_pin"])))
                cached = w[f"{side}_net"]
                w[f"{side}_net_mbs"] = cached
                if live is not None:
                    w[f"{side}_net"] = live
                if live != cached:
                    stale.append({"side": side, "pin": w[f"{side}_pin"],
                                  "board": lk[f"{side}_board"],
                                  "connector": lk[f"{side}_connector"],
                                  "mbs": cached, "schematic": live})
        lk["mbs_stale"] = stale


def bundle_links(links: list[dict]) -> list[dict]:
    """Summarise each board-to-board connector pair by signal function."""
    out = []
    for lk in links:
        groups = Counter()
        for w in lk["wires"]:
            n = w["a_net"] or w["b_net"] or "?"
            groups[net_domain(n)] += 1
        out.append({
            **{k: lk[k] for k in
               ("designator", "a_board", "a_connector", "b_board", "b_connector", "wire_count")},
            "by_domain": dict(groups),
            "nets": sorted({(w["a_net"] or w["b_net"] or "?") for w in lk["wires"]}),
            "wires": lk["wires"],
            "mbs_stale": lk.get("mbs_stale", []),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    argv, ref = project.take_project_arg(list(argv if argv is not None else sys.argv[1:]))
    project.use(ref)
    mbs = next(P.raw.glob("*.MbsDoc"), None)
    system = (parse_mbs(mbs) if mbs else
              {"modules": [], "links": [], "conflicts": []})
    boards = [build_board(key, cfg["title"]) for key, cfg in BOARD_TABLE.items()]

    # map multi-board module titles onto the board models
    title_to_key = {b["title"]: b["key"] for b in boards}
    for m in system["modules"]:
        m["board_key"] = title_to_key.get(m["title"])

    # Resolve every inter-board connector to the sheet it is drawn on, so the
    # diagram can land the link on the right functional block rather than on
    # the board as a whole.
    by_title = {b["title"]: b for b in boards}
    refresh_link_nets(system["links"], by_title)
    links = bundle_links(system["links"])
    linked_connectors: dict[str, set[str]] = defaultdict(set)
    for lk in links:
        for side in ("a", "b"):
            board = by_title.get(lk[f"{side}_board"])
            conn = lk[f"{side}_connector"]
            comp = board["components"].get(conn) if board else None
            lk[f"{side}_block"] = comp["sheet"] if comp else None
            lk[f"{side}_room"] = comp["room"] if comp else None
            if board and comp:
                linked_connectors[board["key"]].add(conn)

    # A pin whose schematic net no longer matches the multi-board document
    # was changed on its board after the last sync -- often a swap made on one
    # board only, which leaves the two ends carrying different signals.
    for lk in links:
        if not lk["mbs_stale"]:
            continue
        pins = sorted({str(x["pin"]) for x in lk["mbs_stale"]}, key=lambda v: (len(v), v))
        rows = []
        for w in lk["wires"]:
            if str(w["a_pin"]) in pins or str(w["b_pin"]) in pins:
                rows.append(f"pin {w['a_pin']}/{w['b_pin']}: "
                            f"{lk['a_board']} {w['a_net']} (MbsDoc {w['a_net_mbs']}) <-> "
                            f"{lk['b_board']} {w['b_net']} (MbsDoc {w['b_net_mbs']})")
        issue = {
            "severity": "warning",
            "kind": "multiboard_out_of_sync",
            "where": f"{lk['designator']} {lk['a_connector']} <-> {lk['b_connector']}",
            "detail": "a connector pin's net differs from the multi-board document, "
                      "so a board changed after the last sync. Check the two ends "
                      "still carry the same signal, then update the .MbsDoc: "
                      + "; ".join(rows),
        }
        for side in ("a", "b"):
            board = by_title.get(lk[f"{side}_board"])
            if board is not None and any(x["side"] == side for x in lk["mbs_stale"]):
                board["issues"].append(issue)

    # Connectors that no inter-board link touches are the system's edges:
    # DC input, motor phase lugs, encoder, CAN, JTAG, GLV supply.
    for b in boards:
        used = linked_connectors[b["key"]]
        ext = []
        for des, c in b["components"].items():
            if c["kind"] != "connector" or des in used:
                continue
            nets = sorted({
                n["net"] for n in
                [{"net": name} for name, rec in b["nets"].items()
                 if any(nd["designator"] == des for nd in rec["nodes"])]
            })
            ext.append({
                "designator": des,
                "logical_designator": c["logical_designator"],
                "room": c["room"],
                "comment": c["comment"],
                "sheet": c["sheet"],
                "pin_count": c["pin_count"],
                "nets": nets,
            })
        b["external_interfaces"] = sorted(ext, key=lambda e: e["designator"])

    # A single-board project has no multi-board schematic, so the system level
    # is simply the one board -- it is not an error, and the model keeps the
    # same shape so everything downstream works unchanged.
    model = {
        "schema": "altium-system/2",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_mbs": mbs.name if mbs else None,
        "system": {
            "name": (mbs.stem if mbs else
                     (P.spec["title"] if P.spec else "design")),
            "kind": "multiboard" if mbs else "single-board",
            "board_count": len(boards),
            "modules": system["modules"],
        },
        "boards": boards,
        "interboard_links": links,
        "conflicts": system["conflicts"],
    }

    out_json = P.out / "system_model.json"
    atomic_write_text(out_json, model_io.dumps(model))

    out_md = P.out / "system_model.md"
    atomic_write_text(out_md, render_markdown(model))

    print(f"wrote {out_json.name} ({out_json.stat().st_size:,} bytes)")
    print(f"wrote {out_md.name} ({out_md.stat().st_size:,} bytes)")
    for b in boards:
        cc = b["cross_check"]
        status = "ok"
        if cc.get("netlist_present"):
            if cc["only_in_netlist"] or cc["only_in_model"]:
                status = (f"MISMATCH netlist-only={len(cc['only_in_netlist'])} "
                          f"model-only={len(cc['only_in_model'])}")
        print(f"  {b['title']:<14} {b['component_count']:>4} parts "
              f"{b['net_count']:>4} nets  cross-check: {status}")
    n_issues = sum(len(b["issues"]) for b in boards)
    print(f"  {n_issues} issue(s) flagged -- see system_model.md")
    return 0


def render_markdown(model: dict) -> str:
    L: list[str] = []
    add = L.append
    add(f"# {model['system']['name']} -- system model")
    add("")
    src = (f"`{model['source_mbs']}` and the per-board extraction"
           if model.get("source_mbs") else "the Altium project files")
    add(f"Generated {model['generated_utc']} from {src}. "
        f"Do not edit by hand; it is rebuilt whenever the design changes.")
    add("")

    add("## Boards")
    add("")
    add("| Board | Project | Sheets | Parts | Nets |")
    add("|---|---|---:|---:|---:|")
    mods = {m["board_key"]: m for m in model["system"]["modules"]}
    for b in model["boards"]:
        m = mods.get(b["key"], {})
        add(f"| {b['title']} | `{m.get('project_name', '?')}` | {b['sheet_count']} "
            f"| {b['component_count']} | {b['net_count']} |")
    add("")

    add("## Board-to-board links")
    add("")
    for lk in model["interboard_links"]:
        dom = ", ".join(f"{k}:{v}" for k, v in sorted(lk["by_domain"].items()))
        add(f"- **{lk['designator']}**  {lk['a_board']} `{lk['a_connector']}` "
            f"<-> {lk['b_board']} `{lk['b_connector']}`  "
            f"({lk['wire_count']} wires; {dom})")
        add(f"  - nets: {', '.join(n for n in lk['nets'] if n)}")
    add("")

    add("## System edges (connectors with no board-to-board link)")
    add("")
    for b in model["boards"]:
        ext = b.get("external_interfaces") or []
        if not ext:
            continue
        add(f"**{b['title']}**")
        add("")
        for e in ext:
            nets = ", ".join(e["nets"][:8]) or "-"
            add(f"- `{e['designator']}` {e['comment']} ({e['pin_count']} pins, "
                f"{e['sheet'].replace('.SchDoc', '')}): {nets}")
        add("")

    for b in model["boards"]:
        add(f"## {b['title']}")
        add("")
        for blk in b["blocks"]:
            rooms = f"  x{len(blk['rooms'])} ({', '.join(blk['rooms'])})" if blk["rooms"] else ""
            add(f"### {blk['name']}{rooms}")
            add(f"{blk['component_count']} parts"
                + (f"; connectors: {', '.join(blk['connectors'])}" if blk["connectors"] else ""))
            if blk["key_parts"]:
                add("")
                for kp in blk["key_parts"]:
                    mpn = f" ({kp['mpn']})" if kp["mpn"] and kp["mpn"] != kp["comment"] else ""
                    add(f"- `{kp['designator']}` {kp['comment']}{mpn}")
            add("")
        rails = [r for r in b["power_rails"] if r["node_count"] >= 3]
        if rails:
            add(f"### {b['title']} -- power rails")
            add("")
            for r in rails[:16]:
                src = f"  from {', '.join(r['likely_sources'])}" if r["likely_sources"] else ""
                add(f"- `{r['net']}` ({r['domain']}, {r['node_count']} pins, "
                    f"{len(r['sheets'])} sheet(s)){src}")
            add("")
        if b["block_links"]:
            add(f"### {b['title']} -- signals between sheets")
            add("")
            for bl in b["block_links"]:
                a = bl["a"].replace(".SchDoc", "")
                bb = bl["b"].replace(".SchDoc", "")
                add(f"- {a} <-> {bb} ({bl['net_count']}): "
                    f"{', '.join(bl['nets'][:14])}"
                    + (" ..." if bl["net_count"] > 14 else ""))
            add("")

    issues = [(b["title"], i) for b in model["boards"] for i in b["issues"]]
    add("## Flagged for review")
    add("")
    if not issues:
        add("Nothing flagged.")
    else:
        for title, i in sorted(issues, key=lambda x: x[1]["severity"]):
            add(f"- **{i['severity']}** [{title}] {i['kind']}: {i['where']} -- {i['detail']}")
    add("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
