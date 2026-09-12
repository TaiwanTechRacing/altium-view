#!/usr/bin/env python3
"""
Build a netlist from parsed .SchDoc geometry, without Altium.

Connectivity is geometric: wires, pin ends, junctions, net labels, power
objects and ports are unioned by coincidence, then each net is named by the
same precedence Altium uses. Hierarchy and repeated channels are expanded on
top of that.

Correctness here is not assumed -- `validate_offline.py` diffs the result
against the netlists Altium itself exported, node by node.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from altium_sch import Sheet, parse_sheet, rint


class DSU:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        self.p.setdefault(a, a)
        root = a
        while self.p[root] != root:
            root = self.p[root]
        while self.p[a] != root:
            self.p[a], a = root, self.p[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def on_segment(p, a, b, tol: int = 0) -> bool:
    """Is point p on segment a-b? Schematic wires are axis-aligned in practice,
    but diagonal segments are handled with an exact cross-product test."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > tol:
        return False
    return (min(ax, bx) - tol <= px <= max(ax, bx) + tol
            and min(ay, by) - tol <= py <= max(ay, by) + tol)


def sheet_nets(sheet: Sheet) -> list[dict]:
    """Union everything electrically touching, then name the resulting groups."""
    dsu = DSU()

    # A No-ERC cross says a pin is deliberately left open. It changes no
    # connectivity, so it is carried as a flag on the node rather than folded
    # into the union -- but without it an intentional open pin and a forgotten
    # one look identical downstream.
    nc_points = {(d["x"], d["y"]) for d in sheet.no_erc if d["active"]}

    wire_keys = []
    for wi, pts in enumerate(sheet.wires):
        key = ("w", wi)
        wire_keys.append(key)
        dsu.find(key)

    def wire_hit(pt) -> list:
        """Wires whose path passes through pt."""
        hits = []
        for wi, pts in enumerate(sheet.wires):
            for a, b in zip(pts, pts[1:]):
                if on_segment(pt, a, b):
                    hits.append(("w", wi))
                    break
        return hits

    # wire-to-wire: touching at an endpoint, or anywhere a junction is drawn.
    # Two wires merely crossing without a junction are NOT connected, which is
    # exactly how Altium reads them.
    for wi, pts in enumerate(sheet.wires):
        for end in (pts[0], pts[-1]):
            for other in wire_hit(end):
                if other != ("w", wi):
                    dsu.union(("w", wi), other)
    for j in sheet.junctions:
        hits = wire_hit(j)
        for h in hits[1:]:
            dsu.union(hits[0], h)

    # Pins are keyed by the component's record index, not its designator: two
    # parts sharing an un-annotated designator such as 'R?' would otherwise be
    # unioned into one node and silently merge two different nets.
    pin_at: dict[tuple[int, int], list] = defaultdict(list)
    for comp in sheet.components:
        if not comp.designator:
            continue
        for pin in comp.pins:
            key = ("p", comp.index, pin.designator)
            dsu.find(key)
            end = pin.end
            pin_at[end].append(key)
            for w in wire_hit(end):
                dsu.union(key, w)
    # pins butted straight onto each other with no wire between them
    for _, keys in pin_at.items():
        for k in keys[1:]:
            dsu.union(keys[0], k)

    # Every non-pin terminal is registered at its candidate points first, then
    # unioned in a second pass, so two terminals sitting directly on each other
    # -- a port whose edge lands on a power object, with no wire between them --
    # connect regardless of the order they appear in the file.
    terminals: list[tuple] = []          # (key, [points], kind, text)
    for i, lab in enumerate(sheet.net_labels):
        terminals.append((("l", i), [(lab["x"], lab["y"])], "net_label", lab["text"]))
    for i, pw in enumerate(sheet.powers):
        terminals.append((("g", i), [(pw["x"], pw["y"])], "power", pw["text"]))
    for i, pt in enumerate(sheet.ports):
        # A port is a shape, not a point: its contacts sit at the ends of its
        # box, and which end is live depends on the arrow style, so offer both.
        w = pt["width"]
        pts = [(pt["x"], pt["y"]), (pt["x"] + w, pt["y"]), (pt["x"] - w, pt["y"])]
        terminals.append((("t", i), pts, "port", pt["name"]))
    # Sheet entries are the parent side of a child sheet's ports. They carry no
    # coordinates: an entry sits on the sheet symbol's edge, one 10-unit step
    # per DistanceFromTop, so the position has to be reconstructed.
    for sym in sheet.sheet_symbols:
        for ent in sym.entries:
            x = sym.x + (0 if ent["side"] == 0 else sym.xsize)
            y = sym.y - ent["distance_from_top"] * 10
            terminals.append((("e", sym.index, ent["name"]), [(rint(x), rint(y))],
                              "sheet_entry", (sym.index, ent["name"])))

    point_keys: dict = defaultdict(list)
    for key, pts, _, _ in terminals:
        dsu.find(key)
        for p in pts:
            point_keys[p].append(key)

    labels: dict = defaultdict(set)
    powers: dict = defaultdict(set)
    ports: dict = defaultdict(set)
    entries: dict = defaultdict(set)
    orphans = []
    bucket = {"net_label": labels, "power": powers, "port": ports,
              "sheet_entry": entries}

    for key, pts, kind, text in terminals:
        hit = False
        for p in pts:
            for w in wire_hit(p):
                dsu.union(key, w)
                hit = True
            for k in pin_at.get(p, []):
                dsu.union(key, k)
                hit = True
            for k in point_keys.get(p, []):
                if k != key:
                    dsu.union(key, k)
                    hit = True
        if hit or kind == "sheet_entry":
            bucket[kind][dsu.find(key)].add(text)
        else:
            orphans.append((kind, text, pts[0]))

    # A cross that landed on nothing suppresses no warning and marks no
    # intent -- usually it was dragged off its pin. Report it like any other
    # identifier attached to nothing.
    pin_ends = set(pin_at)
    wire_points = {pt for w in sheet.wires for pt in w}
    for d in sheet.no_erc:
        pt = (d["x"], d["y"])
        if pt not in pin_ends and pt not in wire_points and not wire_hit(pt):
            orphans.append(("no_erc", "", pt))

    # A net label is a net identifier, not a wire: the same text twice on one
    # sheet is one net even with no copper between the two places. The same
    # goes for ports. Missing this splits a wired-OR fault line into one net
    # per label.
    for store in (labels, ports, powers):
        by_text: dict = defaultdict(list)
        for root, texts in list(store.items()):
            for t in texts:
                by_text[t].append(root)
        for t, roots in by_text.items():
            for r in roots[1:]:
                dsu.union(roots[0], r)

    # collect groups
    groups: dict = defaultdict(lambda: {"nodes": [], "labels": set(),
                                        "powers": set(), "ports": set(),
                                        "entries": set()})
    for comp in sheet.components:
        if not comp.designator:
            continue
        for pin in comp.pins:
            key = ("p", comp.index, pin.designator)
            node = {
                "designator": comp.designator,
                "pin": pin.designator,
                "pin_name": pin.name,
            }
            if pin.end in nc_points:
                node["no_erc"] = True
            groups[dsu.find(key)]["nodes"].append(node)
    # roots are re-resolved here: the same-name merge above moved some of them
    for store, field_name in ((labels, "labels"), (powers, "powers"),
                              (ports, "ports"), (entries, "entries")):
        for root, values in store.items():
            groups[dsu.find(root)][field_name] |= values

    nets = []
    for root, g in groups.items():
        if not g["nodes"] and not (g["labels"] or g["powers"] or g["ports"]
                                   or g["entries"]):
            continue
        nets.append({
            "nodes": g["nodes"],
            "labels": sorted(g["labels"]),
            "powers": sorted(g["powers"]),
            "ports": sorted(g["ports"]),
            "entries": sorted(g["entries"]),
            "sheet": sheet.name,
        })
    return nets, orphans


def auto_name(nodes: list[dict]) -> str:
    """
    Altium's fallback name for an unlabelled net: Net<designator>_<pin> taken
    from the alphabetically first node.
    """
    if not nodes:
        return "?"
    first = sorted(nodes, key=lambda n: (n["designator"], n["pin"]))[0]
    return f"Net{first['designator']}_{first['pin']}"


def net_name(net: dict) -> tuple[str, str]:
    """Name and how it was chosen -- power port, net label, port, or fallback."""
    if net["powers"]:
        return sorted(net["powers"])[0], "power"
    if net["labels"]:
        return sorted(net["labels"])[0], "label"
    if net["ports"]:
        return sorted(net["ports"])[0], "port"
    return auto_name(net["nodes"]), "auto"


def build_board(sheet_paths: list[Path], flat: bool) -> dict:
    """
    Netlist one board.

    Two scoping rules decide every net name here.

    `flat` mirrors the project's hierarchy mode: in a flat project a net label
    is a global identifier, so the same text on two sheets is one net.

    A sheet placed more than once is a multi-channel sheet. Altium gives each
    channel its own copy of every net inside it and appends the room name --
    and that applies to power objects too, not only labels, which is why the
    isolated gate-drive supplies come out as GND_U_PHASE rather than one shared
    GND. The exception is a net that leaves the channel through a port matched
    to a sheet entry: that one keeps the parent's identity.
    """
    sheets = {p.name: parse_sheet(p) for p in sheet_paths}
    roots = [s for s in sheets.values() if s.has_children]

    placements: dict[str, list] = defaultdict(list)
    for r in roots:
        for sym in r.sheet_symbols:
            placements[sym.file_name].append((r, sym))

    all_nets: list[dict] = []
    orphans: list = []
    cache: dict[str, tuple] = {}

    def nets_of(sheet: Sheet):
        if sheet.name not in cache:
            cache[sheet.name] = sheet_nets(sheet)
        return cache[sheet.name]

    def merge_key(name, how, sheet_name, room, index):
        """Nets sharing a merge key are one net."""
        if room is None and (how == "power" or (flat and how == "label")):
            return ("global", name)
        if room is not None:
            return ("chan", sheet_name, room, name)
        return ("sheet", sheet_name, index)

    def record(name, how, sheet_name, room, nodes, key):
        all_nets.append({"name": name, "named_by": how, "sheet": sheet_name,
                         "room": room, "nodes": nodes, "key": key})

    handled: set[str] = set()

    # parent sheets, and the nets their sheet entries sit on
    entry_net: dict[tuple, tuple] = {}    # (sym.index, entry name) -> parent net key
    for root in roots:
        nets, orph = nets_of(root)
        orphans.extend((root.name, None, *o) for o in orph)
        handled.add(root.name)
        for i, n in enumerate(nets):
            name, how = net_name(n)
            key = merge_key(name, how, root.name, None, i)
            record(name, how, root.name, None, list(n["nodes"]), key)
            for ent in n["entries"]:
                entry_net[ent] = key

    # child sheets, once per placement
    for file_name, places in placements.items():
        child = sheets.get(file_name)
        if child is None:
            continue
        handled.add(child.name)
        repeated = len(places) > 1
        nets, orph = nets_of(child)
        for root, sym in places:
            room = sym.name if repeated else None
            orphans.extend((child.name, room, *o) for o in orph)
            entry_names = {e["name"] for e in sym.entries}
            for i, n in enumerate(nets):
                name, how = net_name(n)
                nodes = [{**nd,
                          "designator": f"{nd['designator']}_{room}" if room
                          else nd["designator"]}
                         for nd in n["nodes"]]
                crossing = sorted(set(n["ports"]) & entry_names)
                if crossing:
                    # The net leaves the channel through a sheet entry, so it
                    # is the parent's net and keeps the parent's name -- this
                    # is why the shared DC link is one net rather than three.
                    parent = entry_net.get((sym.index, crossing[0]))
                    if parent is not None:
                        record(name, "entry", child.name, room, nodes, parent)
                        continue
                if room:
                    name = (auto_name(nodes) if how == "auto" else f"{name}_{room}")
                record(name, how, child.name, room, nodes,
                       merge_key(name, how, child.name, room, i))

    for name, sheet in sheets.items():
        if name in handled:
            continue
        nets, orph = nets_of(sheet)
        orphans.extend((sheet.name, None, *o) for o in orph)
        for i, n in enumerate(nets):
            nm, how = net_name(n)
            record(nm, how, sheet.name, None, list(n["nodes"]),
                   merge_key(nm, how, sheet.name, None, i))

    merged: dict = {}
    for n in all_nets:
        slot = merged.setdefault(n["key"], {"name": n["name"],
                                            "named_by": n["named_by"],
                                            "room": n["room"], "sheets": set(),
                                            "nodes": []})
        if n["named_by"] != "entry":
            # a net folded in from a channel never renames its parent
            slot["name"] = n["name"]
            slot["named_by"] = n["named_by"]
        slot["nodes"].extend(n["nodes"])
        slot["sheets"].add(n["sheet"])

    out = []
    for m in merged.values():
        m["sheets"] = sorted(m["sheets"])
        out.append(m)
    return {"nets": out, "sheets": sheets, "orphans": orphans,
            "child_sheets": sorted(placements)}
