#!/usr/bin/env python3
"""
Produce raw/ straight from the Altium files on disk -- no MCP, no Altium running.

Writes the same shape the MCP extraction produced, so everything downstream
(build_system_model.py, render_system.py) is unchanged and the two paths can be
diffed against each other.

Also writes raw/provenance.json: every input file with its size, mtime and
SHA-256, so a reader can see exactly which bytes the model was built from.

Run:  python extract_offline.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import project
from boards import BOARDS, sheet_paths
from project import P
from netlist_offline import build_board

ROOT = Path(__file__).resolve().parent



def resolve_special(text: str, params: dict) -> str:
    """Altium 'special strings': a Comment of '=Value' means "show the Value
    parameter". Libraries use this a lot, and left unresolved every such part
    reads as '=Value' -- useless for a review. Resolve up to two levels."""
    lower = {k.lower(): v for k, v in (params or {}).items()}
    for _ in range(2):
        if not text or not text.startswith("=") or len(text) < 2:
            break
        hit = lower.get(text[1:].strip().lower())
        if hit is None or hit == text:
            break
        text = hit
    return text

def digest(path: Path) -> dict:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    st = path.stat()
    return {
        "path": str(path),
        "bytes": st.st_size,
        "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                .isoformat(timespec="seconds"),
        "sha256": h.hexdigest(),
    }


def board_components(board_key: str) -> tuple[dict, list[dict]]:
    cfg = BOARDS[board_key]
    paths = sheet_paths(board_key)
    result = build_board(paths, flat=cfg["flat"])
    sheets = result["sheets"]

    # (designator, part uid, pin) -> net, from the netlist we just built.
    # The uid keeps two parts that share a designator apart: keyed by
    # designator alone, the second part's pins overwrite the first's.
    node_net: dict[tuple[str, str, str], str] = {}
    node_nc: set[tuple[str, str, str]] = set()
    for net in result["nets"]:
        for nd in net["nodes"]:
            k = (nd["designator"], nd.get("uid", ""), nd["pin"])
            node_net[k] = net["name"]
            if nd.get("no_erc"):
                node_nc.add(k)

    # which rooms each child sheet was placed into
    rooms: dict[str, list[str | None]] = {}
    for s in sheets.values():
        for sym in s.sheet_symbols:
            rooms.setdefault(sym.file_name, []).append(sym.name)
    for name in list(rooms):
        if len(rooms[name]) == 1:
            rooms[name] = [None]          # placed once: no channel suffix

    out: list[dict] = []
    for sheet_name, sheet in sheets.items():
        for room in rooms.get(sheet_name, [None]):
            for comp in sheet.components:
                if not comp.designator:
                    continue
                des = f"{comp.designator}_{room}" if room else comp.designator
                out.append({
                    "designator": des,
                    "comment": resolve_special(comp.comment, comp.parameters),
                    "unique_id": comp.unique_id,
                    "part_id": comp.part_id,
                    "sections": comp.sections,
                    "footprint": comp.footprint,
                    "lib_ref": comp.lib_ref,
                    "sheet": sheet_name,
                    "parameters": dict(comp.parameters),
                    "pins": [{
                        "pin": p.designator,
                        "name": p.name,
                        "net": node_net.get((des, comp.unique_id, p.designator)),
                        "no_erc": (des, comp.unique_id, p.designator) in node_nc,
                    } for p in comp.pins],
                })

    meta = {n["name"]: {"named_by": n.get("named_by"), "room": n.get("room")}
            for n in result["nets"]}
    payload = {
        "net_meta": meta,
        "components": out,
        "matched": len(out),
        "requested": len(out),
        "not_found": [],
        "_source": "offline (altium_sch + netlist_offline)",
    }
    return payload, result["orphans"]


def write_netlist(board_key: str, path: Path) -> int:
    """Write a PADS netlist so the offline path can be diffed against Altium's."""
    cfg = BOARDS[board_key]
    result = build_board(sheet_paths(board_key), flat=cfg["flat"])
    lines = ["*PADS-PCB*", "*NET*"]
    for net in sorted(result["nets"], key=lambda n: n["name"]):
        if not net["nodes"]:
            continue
        lines.append(f"*SIGNAL* {net['name']}")
        nodes = sorted({f"{n['designator']}.{n['pin']}" for n in net["nodes"]})
        lines.append(" ".join(nodes))
    lines.append("*END*")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return len([n for n in result["nets"] if n["nodes"]])


def main(argv: list[str] | None = None) -> int:
    argv, ref = project.take_project_arg(list(argv if argv is not None else sys.argv[1:]))
    project.use(ref)
    P.raw.mkdir(parents=True, exist_ok=True)
    inputs: list[dict] = []

    if P.mbs and P.mbs.exists():
        shutil.copyfile(P.mbs, P.raw / P.mbs.name)
        inputs.append(digest(P.mbs))
    elif P.mbs:
        print(f"warning: {P.mbs} not found -- the system level will be missing",
              file=sys.stderr)

    all_orphans = []
    for key in BOARDS:
        payload, orphans = board_components(key)
        (P.raw / f"{key}_components.json").write_text(
            json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        n_nets = write_netlist(key, P.raw / f"{key}_offline.net")
        for p in sheet_paths(key):
            inputs.append(digest(p))
        inputs.append(digest(BOARDS[key]["project"]))
        all_orphans.extend((key, *o) for o in orphans)
        print(f"{key:<14} {payload['matched']:>4} components, {n_nets:>4} nets")

    prov = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "offline read of Altium source files",
        "tooling": {"altium_sch": "SchDoc record reader",
                    "netlist_offline": "geometric netlister"},
        "inputs": sorted(inputs, key=lambda d: d["path"]),
        "unattached_identifiers": [list(map(str, o)) for o in all_orphans],
    }
    (P.raw / "provenance.json").write_text(
        json.dumps(prov, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(inputs)} source file(s) hashed into raw/provenance.json")
    if all_orphans:
        print(f"{len(all_orphans)} net identifier(s) attached to nothing:")
        for o in all_orphans[:10]:
            print(f"   {o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
