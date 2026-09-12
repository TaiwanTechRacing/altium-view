#!/usr/bin/env python3
"""
Read and write system_model.json.

The model is written in a compact form and expanded back in memory, so every
consumer sees the same dictionary it always did while the file on disk stops
paying for its own repetition:

  * a sheet name is stored once per board and referred to by index -- the full
    name was previously repeated on every node, which was a quarter of the file
  * a key that repeats its own value (a component's designator, a net's name)
    is dropped and restored
  * a field at its default (room=None, sections=1, ambiguous=0) is omitted
  * no pretty-printing whitespace

None of this changes what the model says. `read()` of a compact file and
`read()` of the old pretty one produce identical dictionaries.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "altium-system/3"

# fields holding one sheet name, and fields holding a list of them
SHEET_FIELDS = ("sheet",)
SHEET_LISTS = ("sheets",)


def _board_compact(board: dict) -> dict:
    sheets: list[str] = []

    def idx(name):
        if name is None:
            return None
        if name not in sheets:
            sheets.append(name)
        return sheets.index(name)

    b = dict(board)

    comps = {}
    for des, c in board["components"].items():
        c = dict(c)
        c.pop("designator", None)                       # == the key
        if c.get("logical_designator") == des:
            c.pop("logical_designator")
        if c.get("sheets") == [c.get("sheet")]:
            c.pop("sheets")
        for f in SHEET_FIELDS:
            if f in c:
                c[f] = idx(c[f])
        for f in SHEET_LISTS:
            if f in c:
                c[f] = [idx(s) for s in c[f]]
        for field, default in (("room", None), ("sections", 1), ("ambiguous", 0),
                               ("no_connect_pins", []), ("mpn", None),
                               ("manufacturer", None), ("datasheet", None)):
            if c.get(field) == default:
                c.pop(field, None)
        comps[des] = c
    b["components"] = comps

    nets = {}
    for name, rec in board["nets"].items():
        rec = dict(rec)
        rec.pop("name", None)                           # == the key
        rec.pop("node_count", None)                     # == len(nodes)
        if "sheets" in rec:
            rec["sheets"] = [idx(s) for s in rec["sheets"]]
        nodes = []
        for nd in rec["nodes"]:
            nd = dict(nd)
            if "sheet" in nd:
                nd["sheet"] = idx(nd["sheet"])
            if nd.get("room") is None:
                nd.pop("room", None)
            nodes.append(nd)
        rec["nodes"] = nodes
        nets[name] = rec
    b["nets"] = nets

    b["blocks"] = [{**blk, "sheet": idx(blk["sheet"])} for blk in board["blocks"]]
    b["block_links"] = [{**lk, "a": idx(lk["a"]), "b": idx(lk["b"])}
                        for lk in board["block_links"]]
    b["external_interfaces"] = [{**e, "sheet": idx(e["sheet"])}
                                for e in board.get("external_interfaces", [])]
    # new dicts: the shallow copy above still shares these with the caller's
    # model, and rewriting them in place would corrupt the model in memory
    b["power_rails"] = [{**r, "sheets": [idx(s) for s in r.get("sheets", [])]}
                        for r in board.get("power_rails", [])]
    b["sheet_index"] = sheets
    return b


def _board_expand(board: dict) -> dict:
    sheets = board.get("sheet_index")
    if sheets is None:
        return board                                    # already a full model

    def name(i):
        return sheets[i] if isinstance(i, int) else i

    b = dict(board)
    comps = {}
    for des, c in board["components"].items():
        c = dict(c)
        c["designator"] = des
        c.setdefault("logical_designator", des)
        for f in SHEET_FIELDS:
            if f in c:
                c[f] = name(c[f])
        c["sheets"] = [name(s) for s in c["sheets"]] if "sheets" in c else [c.get("sheet")]
        for field, default in (("room", None), ("sections", 1), ("ambiguous", 0),
                               ("no_connect_pins", []), ("mpn", None),
                               ("manufacturer", None), ("datasheet", None)):
            c.setdefault(field, default if not isinstance(default, list) else list(default))
        comps[des] = c
    b["components"] = comps

    nets = {}
    for net_name, rec in board["nets"].items():
        rec = dict(rec)
        rec["name"] = net_name
        rec["sheets"] = [name(s) for s in rec.get("sheets", [])]
        nodes = []
        for nd in rec["nodes"]:
            nd = dict(nd)
            if "sheet" in nd:
                nd["sheet"] = name(nd["sheet"])
            nd.setdefault("room", None)
            nodes.append(nd)
        rec["nodes"] = nodes
        rec["node_count"] = len(nodes)
        nets[net_name] = rec
    b["nets"] = nets

    b["blocks"] = [{**blk, "sheet": name(blk["sheet"])} for blk in board["blocks"]]
    b["block_links"] = [{**lk, "a": name(lk["a"]), "b": name(lk["b"])}
                        for lk in board["block_links"]]
    b["external_interfaces"] = [{**e, "sheet": name(e["sheet"])}
                                for e in board.get("external_interfaces", [])]
    b["power_rails"] = [{**r, "sheets": [name(s) for s in r.get("sheets", [])]}
                        for r in board.get("power_rails", [])]
    b.pop("sheet_index", None)
    return b


def dumps(model: dict) -> str:
    out = dict(model)
    out["schema"] = SCHEMA
    out["boards"] = [_board_compact(b) for b in model["boards"]]
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def loads(text: str) -> dict:
    data = json.loads(text)
    data["boards"] = [_board_expand(b) for b in data["boards"]]
    return data


def read(path: str | Path) -> dict:
    return loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        p = Path(arg)
        full = read(p)
        print(f"{p.name}: {p.stat().st_size:,} bytes on disk, "
              f"{len(json.dumps(full, ensure_ascii=False, indent=2)):,} expanded")
