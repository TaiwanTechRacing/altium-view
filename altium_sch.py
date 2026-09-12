#!/usr/bin/env python3
"""
Offline reader for Altium .SchDoc files.

A SchDoc is an OLE compound file whose `FileHeader` stream is a flat sequence
of length-prefixed, pipe-delimited ASCII records -- so everything the schematic
holds (components, pin coordinates, symbol graphics, wires, labels, power
objects, sheet symbols) can be read without Altium running.

This module only reads and structures. Connectivity is in `netlist_offline.py`.

Requires `olefile` (pure Python): pip install olefile
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import olefile

# Record ids used by the schematic format. Only the ones this pipeline reads
# are named; the rest are kept as raw dicts so nothing is silently dropped.
REC = {
    1: "component", 2: "pin", 4: "label", 5: "bezier", 6: "polyline",
    7: "polygon", 8: "ellipse", 11: "elliptical_arc", 12: "arc", 13: "line",
    14: "rectangle", 15: "sheet_symbol", 16: "sheet_entry", 17: "power",
    18: "port", 22: "no_erc", 25: "net_label", 26: "bus", 27: "wire",
    28: "text_frame", 29: "junction", 30: "image", 31: "sheet",
    32: "sheet_name", 33: "sheet_file_name", 34: "designator", 37: "bus_entry",
    39: "template", 41: "parameter", 43: "warning_sign", 44: "impl_list",
    45: "impl", 46: "impl_param", 47: "impl_pin", 48: "impl_pin2",
}

# PinConglomerate bits 0..1 give the pin direction; the connection end sits one
# PinLength away from the stored location along it.
PIN_DIR = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}


def read_records(path: str | Path) -> list[dict]:
    """Every record in the FileHeader stream, in file order."""
    ole = olefile.OleFileIO(str(path))
    try:
        data = ole.openstream("FileHeader").read()
    finally:
        ole.close()

    out: list[dict] = []
    i = 0
    while i + 4 <= len(data):
        (length,) = struct.unpack("<I", data[i:i + 4])
        i += 4
        text = data[i:i + length].decode("latin-1").rstrip("\x00")
        i += length
        kv: dict[str, str] = {}
        for chunk in text.split("|"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                kv[k] = v
        kv["_index"] = str(len(out))
        out.append(kv)
    if i != len(data):
        raise ValueError(f"{path}: {len(data) - i} trailing bytes -- record walk desynced")
    return out


def recode(s: str) -> str:
    """
    Records are read as latin-1 to keep byte offsets exact, but Altium writes
    text in the authoring machine's codepage -- Chinese library and BOM
    parameters here are GBK. Re-decode only strings that actually carry high
    bytes, so ASCII is never touched.
    """
    if not s or all(ord(ch) < 0x80 for ch in s):
        return s
    raw = s.encode("latin-1", "ignore")
    for enc in ("utf-8", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return s


def owner_of(kv: dict) -> int:
    """
    Resolve a record's OwnerIndex to an absolute record index.

    Altium's OwnerIndex counts from the record after the file header, so it
    always runs exactly one behind this walk's index. Guessing with a +/-1
    search instead attaches parameters to the wrong component on sheets where
    records interleave.
    """
    raw = kv.get("OwnerIndex")
    if raw in (None, ""):
        return -1
    try:
        return int(raw) + 1
    except ValueError:
        return -1


def coord(kv: dict, key: str) -> float:
    """
    Altium stores a coordinate as an integer plus an optional _Frac field in
    hundred-thousandths. Ports in particular carry fractions, and dropping them
    moves a port off the wire it is sitting on.
    """
    base = float(kv.get(key, 0) or 0)
    frac = float(kv.get(key + "_Frac", 0) or 0)
    return base + frac / 100000.0


def rint(v: float) -> int:
    return int(round(v))


_POINT_KEYS = re.compile(r"^(?:X|Y)\d+$")


def primitive_points(kv: dict) -> list[tuple[float, float]]:
    """
    Every (x, y) a graphic record defines.

    Coordinate fields must be matched exactly: a loose 'starts with X' test
    also catches the companion 'X1_Frac' field, whose value is a fraction in
    hundred-thousandths. Treating 70540 as a coordinate blows a resistor's
    bounding box out to 70000 units wide and takes the whole drawing's scale
    with it.
    """
    pts: list[tuple[float, float]] = []
    n = kv.get("LocationCount")
    if n:
        for i in range(1, int(n) + 1):
            if f"X{i}" in kv:
                pts.append((coord(kv, f"X{i}"), coord(kv, f"Y{i}")))
    else:
        for i in range(1, 200):
            if f"X{i}" not in kv:
                break
            pts.append((coord(kv, f"X{i}"), coord(kv, f"Y{i}")))
    if "Location.X" in kv:
        pts.append((coord(kv, "Location.X"), coord(kv, "Location.Y")))
    if "Corner.X" in kv:
        pts.append((coord(kv, "Corner.X"), coord(kv, "Corner.Y")))
    if "Location.X" in kv and kv.get("Radius"):
        cx, cy = coord(kv, "Location.X"), coord(kv, "Location.Y")
        r = float(kv.get("Radius") or 0)
        ry = float(kv.get("SecondaryRadius") or r)
        pts += [(cx - r, cy - ry), (cx + r, cy + ry)]
    return pts


@dataclass
class Pin:
    designator: str
    name: str
    owner: int
    part_id: int
    x: float
    y: float
    length: float
    conglomerate: int
    electrical: int

    @property
    def end(self) -> tuple[int, int]:
        """The electrical end -- what actually touches a wire."""
        dx, dy = PIN_DIR.get(self.conglomerate & 0x03, (1, 0))
        return rint(self.x + dx * self.length), rint(self.y + dy * self.length)


@dataclass
class Component:
    index: int
    lib_ref: str
    design_item_id: str
    description: str
    x: float
    y: float
    part_id: int
    part_count: int
    unique_id: str
    designator: str = ""
    comment: str = ""
    footprint: str = ""
    parameters: dict = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)

    @property
    def sections(self) -> int:
        """
        How many parts the component is divided into.

        Altium stores PartCount as sections + 1 -- a plain resistor records 2,
        a dual comparator 3, a quad op-amp 5. Reading the field as the section
        count makes every passive look multi-part and, worse, makes the four
        sections of one op-amp look like four parts colliding on a designator.
        """
        return max(1, self.part_count - 1)


@dataclass
class SheetSymbol:
    index: int
    x: float
    y: float
    xsize: float
    ysize: float
    unique_id: str
    name: str = ""
    file_name: str = ""
    entries: list[dict] = field(default_factory=list)


@dataclass
class Sheet:
    path: Path
    name: str
    records: list[dict]
    components: list[Component] = field(default_factory=list)
    wires: list[list[tuple[int, int]]] = field(default_factory=list)
    junctions: list[tuple[int, int]] = field(default_factory=list)
    net_labels: list[dict] = field(default_factory=list)
    powers: list[dict] = field(default_factory=list)
    ports: list[dict] = field(default_factory=list)
    sheet_symbols: list[SheetSymbol] = field(default_factory=list)
    graphics: list[dict] = field(default_factory=list)
    no_erc: list[dict] = field(default_factory=list)

    @property
    def has_children(self) -> bool:
        return bool(self.sheet_symbols)


def parse_sheet(path: str | Path) -> Sheet:
    path = Path(path)
    records = read_records(path)
    sheet = Sheet(path=path, name=path.name, records=records)

    by_index: dict[int, Component] = {}
    sym_by_index: dict[int, SheetSymbol] = {}
    impl_list_owner: dict[int, int] = {}   # impl_list record index -> component index

    for kv in records:
        rid = int(kv.get("RECORD", -1) or -1)
        idx = int(kv["_index"])
        kind = REC.get(rid)

        if kind == "component":
            c = Component(
                index=idx,
                lib_ref=recode(kv.get("LibReference", "")),
                design_item_id=kv.get("DesignItemId", ""),
                description=recode(kv.get("ComponentDescription", "")),
                x=coord(kv, "Location.X"), y=coord(kv, "Location.Y"),
                part_id=int(kv.get("CurrentPartId", 1) or 1),
                part_count=int(kv.get("PartCount", 1) or 1),
                unique_id=kv.get("UniqueID", ""),
            )
            sheet.components.append(c)
            by_index[idx] = c

        elif kind == "pin":
            owner = owner_of(kv)
            target = by_index.get(owner)
            if target is not None:
                target.pins.append(Pin(
                    designator=kv.get("Designator", ""),
                    name=recode(kv.get("Name", "")),
                    owner=owner,
                    part_id=int(kv.get("OwnerPartId", 1) or 1),
                    x=coord(kv, "Location.X"), y=coord(kv, "Location.Y"),
                    length=float(kv.get("PinLength", 0) or 0),
                    conglomerate=int(kv.get("PinConglomerate", 0) or 0),
                    electrical=int(kv.get("Electrical", 0) or 0),
                ))

        elif kind == "designator":
            target = by_index.get(owner_of(kv))
            if target is not None:
                target.designator = kv.get("Text", "")

        elif kind == "parameter":
            target = by_index.get(owner_of(kv))
            name, text = recode(kv.get("Name", "")), recode(kv.get("Text", ""))
            if target is not None and name:
                target.parameters[name] = text
                if name == "Comment":
                    target.comment = text

        elif kind == "impl_list":
            comp = by_index.get(owner_of(kv))
            if comp is not None:
                impl_list_owner[idx] = comp.index

        elif kind == "impl":
            # an implementation hangs off the component's implementation list,
            # so resolving it takes two hops, not one
            comp_index = impl_list_owner.get(owner_of(kv))
            target = by_index.get(comp_index) if comp_index is not None else None
            if target is not None and kv.get("ModelType") == "PCBLIB":
                name = kv.get("ModelName", "")
                if kv.get("IsCurrent") == "T" or not target.footprint:
                    target.footprint = name

        elif kind == "wire":
            n = int(kv.get("LocationCount", 0) or 0)
            pts = [(rint(coord(kv, f"X{j}")), rint(coord(kv, f"Y{j}")))
                   for j in range(1, n + 1)]
            if len(pts) >= 2:
                sheet.wires.append(pts)

        elif kind == "junction":
            sheet.junctions.append((rint(coord(kv, "Location.X")),
                                    rint(coord(kv, "Location.Y"))))

        elif kind == "net_label":
            sheet.net_labels.append({
                "x": rint(coord(kv, "Location.X")),
                "y": rint(coord(kv, "Location.Y")),
                "text": recode(kv.get("Text", "")),
                "orientation": int(kv.get("Orientation", 0) or 0),
            })

        elif kind == "no_erc":
            # The red cross an engineer places to say "this pin is meant to be
            # left open". It carries no connectivity, but it is the only record
            # in the file that separates a deliberate open pin from a forgotten
            # one, so it is read and kept.
            sheet.no_erc.append({
                "x": rint(coord(kv, "Location.X")),
                "y": rint(coord(kv, "Location.Y")),
                "active": kv.get("IsActive", "T") != "F",
                "suppress_all": kv.get("SuppressAll", "") == "T",
            })

        elif kind == "power":
            sheet.powers.append({
                "x": rint(coord(kv, "Location.X")),
                "y": rint(coord(kv, "Location.Y")),
                "text": kv.get("Text", ""),
                "style": int(kv.get("Style", 0) or 0),
                "orientation": int(kv.get("Orientation", 0) or 0),
            })

        elif kind == "port":
            sheet.ports.append({
                "x": rint(coord(kv, "Location.X")),
                "y": rint(coord(kv, "Location.Y")),
                "name": recode(kv.get("Name", "")),
                "width": float(kv.get("Width", 0) or 0),
                "alignment": int(kv.get("Alignment", 0) or 0),
                "io_type": int(kv.get("IOType", 0) or 0),
                "style": int(kv.get("Style", 0) or 0),
            })

        elif kind == "sheet_symbol":
            s = SheetSymbol(
                index=idx,
                x=coord(kv, "Location.X"), y=coord(kv, "Location.Y"),
                xsize=coord(kv, "XSize"), ysize=coord(kv, "YSize"),
                unique_id=kv.get("UniqueID", ""),
            )
            sheet.sheet_symbols.append(s)
            sym_by_index[idx] = s

        elif kind in ("sheet_name", "sheet_file_name", "sheet_entry"):
            target = sym_by_index.get(owner_of(kv))
            if target is None:
                continue
            if kind == "sheet_name":
                target.name = kv.get("Text", "")
            elif kind == "sheet_file_name":
                target.file_name = kv.get("Text", "")
            else:
                target.entries.append({
                    "name": kv.get("Name", ""),
                    "distance_from_top": float(kv.get("DistanceFromTop", 0) or 0),
                    "side": int(kv.get("Side", 0) or 0),
                    "io_type": int(kv.get("IOType", 0) or 0),
                })

        elif kind in ("polyline", "line", "rectangle", "arc", "elliptical_arc",
                      "ellipse", "polygon", "bezier", "label", "round_rect"):
            sheet.graphics.append({"kind": kind, "owner": int(kv.get("OwnerIndex", -1) or -1),
                                   "part_id": int(kv.get("OwnerPartId", -1) or -1),
                                   "raw": kv})

    # keep only the pins of the part actually placed
    for c in sheet.components:
        if c.part_count > 1:
            c.pins = [p for p in c.pins if p.part_id in (c.part_id, -1)] or c.pins

    return sheet


def parse_project(sch_paths) -> dict[str, Sheet]:
    return {Path(p).name: parse_sheet(p) for p in sch_paths}


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        s = parse_sheet(p)
        print(f"{s.name}: {len(s.components)} components, {len(s.wires)} wires, "
              f"{len(s.net_labels)} net labels, {len(s.powers)} power objects, "
              f"{len(s.ports)} ports, {len(s.sheet_symbols)} sheet symbols, "
              f"{len(s.no_erc)} no-ERC marker(s)")
        for c in s.components[:6]:
            print(f"   {c.designator:<10} {c.comment:<28} {c.lib_ref:<24} "
                  f"{len(c.pins)} pins  fp={c.footprint}")
