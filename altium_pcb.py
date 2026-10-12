#!/usr/bin/env python3
"""
Read an Altium .PcbDoc without Altium.

A .PcbDoc is an OLE compound file. Each object kind lives in its own storage
(`Tracks6/Data`, `Pads6/Data`, ...). Some are pipe-delimited text records like
a .SchDoc (`Board6`, `Components6`, `Nets6`, `Polygons6`); the electrical
primitives are fixed binary records. The binary layouts below follow KiCad's
Altium importer (pcbnew/pcb_io/altium/altium_parser_pcb.cpp) and were checked
against boards whose geometry is known.

Units: Altium stores 1/10000 mil. Everything returned here is millimetres,
in Altium's own frame (x right, y up), so coordinates match what the PCB
editor shows relative to its absolute origin.

  read_pcb(path) -> dict   (plain JSON-serialisable)

Indices: a primitive refers to its net and component by position in
`Nets6` / `Components6`; 0xFFFF means "none". Those are resolved to names
here so nothing downstream has to know about them.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import olefile

NONE16 = 0xFFFF
MM_PER_UNIT = 0.0254 / 10000.0      # 1 unit = 1/10000 mil


def mm(v: float) -> float:
    return v * MM_PER_UNIT


def mil_text(s: str | None) -> float | None:
    """'1234.5mil' / '3.2mm' -> mm."""
    if s is None:
        return None
    s = s.strip()
    try:
        if s.lower().endswith("mil"):
            return float(s[:-3]) * 0.0254
        if s.lower().endswith("mm"):
            return float(s[:-2])
        return float(s) * 0.0254
    except ValueError:
        return None


# Altium's legacy layer numbering, used by every binary record.
LAYER_NAMES = {1: "Top Layer", 32: "Bottom Layer",
               33: "Top Overlay", 34: "Bottom Overlay",
               35: "Top Paste", 36: "Bottom Paste",
               37: "Top Solder", 38: "Bottom Solder",
               55: "Drill Guide", 56: "Keep-Out Layer",
               73: "Drill Drawing", 74: "Multi-Layer"}
for _i in range(2, 32):
    LAYER_NAMES[_i] = f"Mid-Layer {_i - 1}"
for _i in range(39, 55):
    LAYER_NAMES[_i] = f"Internal Plane {_i - 38}"
for _i in range(57, 73):
    LAYER_NAMES[_i] = f"Mechanical {_i - 56}"

TEXT_LAYER = {"TOP": 1, "BOTTOM": 32, "TOPOVERLAY": 33, "BOTTOMOVERLAY": 34,
              "MULTILAYER": 74, "KEEPOUT": 56}
for _i in range(1, 31):
    TEXT_LAYER[f"MID{_i}"] = _i + 1
for _i in range(1, 17):
    TEXT_LAYER[f"PLANE{_i}"] = _i + 38
    TEXT_LAYER[f"MECHANICAL{_i}"] = _i + 56

PAD_SHAPE = {1: "round", 2: "rect", 3: "octagonal", 9: "roundrect"}


def is_copper(layer: int) -> bool:
    return 1 <= layer <= 32 or 39 <= layer <= 54


# --------------------------------------------------------------------------
# stream readers
# --------------------------------------------------------------------------

class Doc:
    def __init__(self, path: Path):
        self.ole = olefile.OleFileIO(str(path))

    def data(self, storage: str) -> bytes:
        name = f"{storage}/Data"
        if not self.ole.exists(name):
            return b""
        return self.ole.openstream(name).read()

    def text_records(self, storage: str) -> list[dict]:
        """[u32 length][|KEY=VAL|...\\0] records, keys upper-cased."""
        b, i, out = self.data(storage), 0, []
        while i + 4 <= len(b):
            n = struct.unpack_from("<I", b, i)[0]
            i += 4
            raw = b[i:i + n].rstrip(b"\x00")
            i += n
            kv: dict[str, str] = {}
            for field in raw.decode("latin-1").split("|"):
                if "=" in field:
                    k, v = field.split("=", 1)
                    kv[k.upper()] = v
            out.append(kv)
        return out

    def binary_records(self, storage: str):
        """Yield (record_type, [subrecord bytes, ...]).

        A record is one type byte followed by length-prefixed subrecords. Most
        kinds carry exactly one; pads carry five or six and texts two, so the
        subrecords of one record are gathered until the next byte reads as a
        plausible record type again.
        """
        b = self.data(storage)
        i = 0
        while i < len(b):
            rtype = b[i]
            i += 1
            subs = []
            want = {2: 5, 5: 2}.get(rtype, 1)        # pad, text
            while len(subs) < want and i + 4 <= len(b):
                n = struct.unpack_from("<I", b, i)[0]
                i += 4
                subs.append(b[i:i + n])
                i += n
            # newer pads add a sixth (per-layer size) subrecord; it is there
            # when the next byte is not the start of another pad record
            if rtype == 2 and i + 4 <= len(b) and b[i] != 2:
                n = struct.unpack_from("<I", b, i)[0]
                i += 4
                subs.append(b[i:i + n])
                i += n
            yield rtype, subs


# --------------------------------------------------------------------------
# board: stack-up and outline
# --------------------------------------------------------------------------

def parse_stack(board: dict) -> list[dict]:
    """Physical stack from the V9 records, top to bottom, copper + dielectric."""
    out = []
    k = 0
    while f"V9_STACK_LAYER{k}_NAME" in board:
        pre = f"V9_STACK_LAYER{k}_"
        lid = int(board.get(pre + "LAYERID", "0") or 0)
        kind_byte, legacy = (lid >> 16) & 0xFF, lid & 0xFFFF
        if kind_byte == 0x00 and legacy == 0xFFFF:      # bottom copper
            legacy = 32
        name = board[pre + "NAME"]
        rec = {"name": name}
        if kind_byte == 0x00 or kind_byte == 0x01:      # copper / plane
            if is_copper(legacy):
                rec.update(kind="copper", layer=legacy,
                           copper_mm=mil_text(board.get(pre + "COPTHICK")))
        if "SOLDER" in name.upper() or "PASTE" in name.upper() or "OVERLAY" in name.upper():
            k += 1
            continue
        if pre + "DIELHEIGHT" in board:
            rec.update(kind="dielectric",
                       height_mm=mil_text(board.get(pre + "DIELHEIGHT")),
                       er=float(board.get(pre + "DIELCONST", "0") or 0),
                       material=board.get(pre + "DIELMATERIAL"))
        if "kind" in rec:
            out.append(rec)
        k += 1
    return out


def arc_points(cx, cy, r, sa, ea, start_near=None, step_deg=10.0):
    """Points along a CCW arc from sa to ea, reversed when the path being
    built arrives at the ea end -- an outline may run either way round."""
    sweep = (ea - sa) % 360 or 360
    steps = max(2, int(sweep / step_deg))
    pts = [[cx + r * math.cos(math.radians(sa + sweep * k / steps)),
            cy + r * math.sin(math.radians(sa + sweep * k / steps))] for k in range(steps + 1)]
    if start_near is not None:
        d0 = math.dist(pts[0], start_near)
        d1 = math.dist(pts[-1], start_near)
        if d1 < d0:
            pts.reverse()
    return pts


def outline_points(rec: dict, prefix: str = "") -> list[list[float]]:
    """Vertex list (with arcs flattened) from VX0/VY0/KIND0... text keys."""
    pts = []
    j = 0
    while f"{prefix}VX{j}" in rec:
        x = mil_text(rec[f"{prefix}VX{j}"])
        y = mil_text(rec[f"{prefix}VY{j}"])
        kind = rec.get(f"{prefix}KIND{j}", "0")
        if kind == "1":                                     # arc segment
            cx = mil_text(rec.get(f"{prefix}CX{j}"))
            cy = mil_text(rec.get(f"{prefix}CY{j}"))
            r = mil_text(rec.get(f"{prefix}R{j}"))
            sa = float(rec.get(f"{prefix}SA{j}", "0") or 0)
            ea = float(rec.get(f"{prefix}EA{j}", "0") or 0)
            pts.append([x, y])
            if r and cx is not None:
                pts.extend(arc_points(cx, cy, r, sa, ea, (x, y))[1:-1])
        else:
            pts.append([x, y])
        j += 1
    return pts


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def _common(d: bytes):
    """layer, net, polygon, component -- the shared head of most records."""
    layer = d[0]
    net, poly, comp = struct.unpack_from("<HHH", d, 3)
    return layer, net, poly, comp


def parse_tracks(doc: Doc) -> list[dict]:
    out = []
    for rtype, subs in doc.binary_records("Tracks6"):
        d = subs[0]
        layer, net, poly, comp = _common(d)
        x1, y1, x2, y2, w = struct.unpack_from("<5i", d, 13)
        out.append({"layer": layer, "net": net, "polygon": poly, "component": comp,
                    "x1": mm(x1), "y1": mm(y1), "x2": mm(x2), "y2": mm(y2),
                    "width": mm(w), "keepout": d[2] == 2})
    return out


def parse_arcs(doc: Doc) -> list[dict]:
    out = []
    for rtype, subs in doc.binary_records("Arcs6"):
        d = subs[0]
        layer, net, poly, comp = _common(d)
        cx, cy, r = struct.unpack_from("<3i", d, 13)
        sa, ea = struct.unpack_from("<2d", d, 25)
        w = struct.unpack_from("<i", d, 41)[0]
        out.append({"layer": layer, "net": net, "polygon": poly, "component": comp,
                    "cx": mm(cx), "cy": mm(cy), "r": mm(r), "start": sa, "end": ea,
                    "width": mm(w), "keepout": d[2] == 2})
    return out


def parse_vias(doc: Doc) -> list[dict]:
    out = []
    for rtype, subs in doc.binary_records("Vias6"):
        d = subs[0]
        net = struct.unpack_from("<H", d, 3)[0]
        x, y, dia, hole = struct.unpack_from("<4i", d, 13)
        out.append({"net": net, "x": mm(x), "y": mm(y), "diameter": mm(dia),
                    "hole": mm(hole), "from": d[29], "to": d[30]})
    return out


def parse_fills(doc: Doc) -> list[dict]:
    out = []
    for rtype, subs in doc.binary_records("Fills6"):
        d = subs[0]
        layer, net, _, comp = _common(d)
        x1, y1, x2, y2 = struct.unpack_from("<4i", d, 13)
        rot = struct.unpack_from("<d", d, 29)[0]
        out.append({"layer": layer, "net": net, "component": comp,
                    "x1": mm(x1), "y1": mm(y1), "x2": mm(x2), "y2": mm(y2),
                    "rotation": rot, "keepout": d[2] == 2})
    return out


def parse_pads(doc: Doc) -> list[dict]:
    out = []
    for rtype, subs in doc.binary_records("Pads6"):
        if len(subs) < 5:
            continue
        s1 = subs[0]
        name = s1[1:1 + s1[0]].decode("latin-1") if s1 else ""
        d = subs[4]
        layer = d[0]
        net = struct.unpack_from("<H", d, 3)[0]
        comp = struct.unpack_from("<H", d, 7)[0]
        x, y, tx, ty, mx, my, bx, by, hole = struct.unpack_from("<9i", d, 13)
        shape = d[49]
        rot = struct.unpack_from("<d", d, 52)[0]
        out.append({"name": name, "layer": layer, "net": net, "component": comp,
                    "x": mm(x), "y": mm(y), "sx": mm(tx), "sy": mm(ty),
                    "bsx": mm(bx), "bsy": mm(by),
                    "hole": mm(hole), "shape": PAD_SHAPE.get(shape, str(shape)),
                    "rotation": rot})
    return out


def _props(d: bytes, i: int) -> tuple[dict, int]:
    n = struct.unpack_from("<I", d, i)[0]
    i += 4
    kv = {}
    for field in d[i:i + n].rstrip(b"\x00").decode("latin-1").split("|"):
        if "=" in field:
            k, v = field.split("=", 1)
            kv[k.upper()] = v
    return kv, i + n


def parse_regions(doc: Doc, storage: str, extended: bool) -> list[dict]:
    """Copper regions. `extended` = ShapeBasedRegions6, whose vertices carry arcs."""
    out = []
    for rtype, subs in doc.binary_records(storage):
        d = subs[0]
        layer, net, poly, comp = _common(d)
        holecount = struct.unpack_from("<H", d, 14)[0]
        try:
            props, i = _props(d, 18)
            nv = struct.unpack_from("<I", d, i)[0]
            i += 4
            pts = []
            if extended:
                nv += 1
                # 37-byte vertex. Current files (and KiCad's importer) use
                # is_round u8, x y cx cy radius i32, start end f64; some older
                # files put radius and the angles first. Take whichever layout
                # gives coordinates that lie on a plausible board.
                verts = None
                for layout in ("new", "old"):
                    vs, ok = [], True
                    for k in range(nv):
                        o = i + 37 * k
                        if layout == "new":
                            px, py, cx, cy, r = struct.unpack_from("<5i", d, o + 1)
                            sa, ea = struct.unpack_from("<2d", d, o + 21)
                        else:
                            r = struct.unpack_from("<i", d, o + 1)[0]
                            sa, ea = struct.unpack_from("<2d", d, o + 5)
                            px, py, cx, cy = struct.unpack_from("<4i", d, o + 21)
                        if not (0 < abs(px) + abs(py) and abs(mm(px)) < 3000 and abs(mm(py)) < 3000):
                            ok = False
                            break
                        vs.append((d[o], px, py, cx, cy, r, sa, ea))
                    if ok:
                        verts = vs
                        break
                i += 37 * nv
                if verts is None:
                    continue
                for is_round, px, py, cx, cy, r, sa, ea in verts:
                    if is_round and r:
                        pts.extend(arc_points(mm(cx), mm(cy), mm(r), sa, ea,
                                              pts[-1] if pts else None, 15.0))
                    else:
                        pts.append([mm(px), mm(py)])
            else:
                for _ in range(nv):
                    px, py = struct.unpack_from("<2d", d, i)
                    i += 16
                    pts.append([mm(px), mm(py)])
            holes = []
            for _ in range(holecount):
                hn = struct.unpack_from("<I", d, i)[0]
                i += 4
                h = []
                for _ in range(hn):
                    px, py = struct.unpack_from("<2d", d, i)
                    i += 16
                    h.append([mm(px), mm(py)])
                holes.append(h)
        except struct.error:
            continue
        out.append({"layer": layer, "net": net, "polygon": poly, "component": comp,
                    "kind": int(props.get("KIND", "0") or 0),
                    "cutout": props.get("ISBOARDCUTOUT", "FALSE") == "TRUE",
                    "keepout": d[2] == 2,
                    "points": pts, "holes": holes})
    return out


# --------------------------------------------------------------------------
# the whole board
# --------------------------------------------------------------------------

def physical_designator(c: dict) -> str:
    """Same naming the schematic model uses: '<logical>_<room>' inside a channel."""
    des = c.get("SOURCEDESIGNATOR", "")
    path = c.get("SOURCEHIERARCHICALPATH", "")
    parts = [p for p in path.split("\\") if p]
    return f"{des}_{parts[-1]}" if len(parts) > 1 else des


def polygon_area(pts: list[list[float]]) -> float:
    a = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def read_pcb(path: str | Path) -> dict:
    path = Path(path)
    doc = Doc(path)
    board = (doc.text_records("Board6") or [{}])[0]
    nets = [n.get("NAME", "") for n in doc.text_records("Nets6")]
    comps_raw = doc.text_records("Components6")

    def net_name(i: int) -> str | None:
        return nets[i] if i != NONE16 and i < len(nets) else None

    components = []
    for c in comps_raw:
        components.append({
            "designator": physical_designator(c),
            "logical": c.get("SOURCEDESIGNATOR", ""),
            "footprint": c.get("PATTERN", ""),
            "lib_ref": c.get("SOURCELIBREFERENCE", ""),
            "x": mil_text(c.get("X")), "y": mil_text(c.get("Y")),
            "rotation": float(c.get("ROTATION", "0") or 0),
            "side": "bottom" if c.get("LAYER", "TOP").upper() == "BOTTOM" else "top",
            "unique_id": c.get("SOURCEUNIQUEID", ""),
        })

    def comp_name(i: int) -> str | None:
        return components[i]["designator"] if i != NONE16 and i < len(components) else None

    def resolve(items):
        for it in items:
            if "net" in it:
                it["net"] = net_name(it["net"])
            if "component" in it:
                it["component"] = comp_name(it["component"])
        return items

    pads = resolve(parse_pads(doc))
    tracks = resolve(parse_tracks(doc))
    arcs = resolve(parse_arcs(doc))
    vias = resolve(parse_vias(doc))
    fills = resolve(parse_fills(doc))
    # Newer files store each region twice: plain in Regions6 and with true
    # arcs in ShapeBasedRegions6. Take the shape-based copy and add only the
    # plain records it does not already have.
    shaped = parse_regions(doc, "ShapeBasedRegions6", extended=True)
    plain = parse_regions(doc, "Regions6", extended=False)

    def key(r):
        xs = [p[0] for p in r["points"]] or [0]
        ys = [p[1] for p in r["points"]] or [0]
        return (r["layer"], r["polygon"], round(min(xs), 1), round(min(ys), 1),
                round(max(xs), 1), round(max(ys), 1))

    seen = {key(r) for r in shaped}
    regions = shaped + [r for r in plain if key(r) not in seen]

    polygons = []
    for k, p in enumerate(doc.text_records("Polygons6")):
        ni = int(p.get("NET", "-1") or -1)
        polygons.append({
            "index": k,
            "name": p.get("NAME", ""),
            "layer": TEXT_LAYER.get(p.get("LAYER", "").upper(), 0),
            "net": nets[ni] if 0 <= ni < len(nets) else None,
            "type": p.get("POLYGONTYPE", ""),
            "hatch": p.get("HATCHSTYLE", ""),
            "points": outline_points(p),
        })

    # a poured region carries no net of its own: it belongs to its polygon
    for r in regions:
        r["net"] = net_name(r["net"])
        r["component"] = comp_name(r["component"])
        pi = r["polygon"]
        if pi != NONE16 and pi < len(polygons):
            if r["net"] is None:
                r["net"] = polygons[pi]["net"]
            r["polygon"] = pi
        else:
            r["polygon"] = None

    stack = parse_stack(board)
    copper = [s for s in stack if s.get("kind") == "copper"]
    for n, s in enumerate(copper, 1):
        s["index"] = n                              # L1 = top, Ln = bottom
    layer_alias = {s["layer"]: f"L{s['index']}" for s in copper}

    outline = outline_points(board)
    xs = [p[0] for p in outline] or [0]
    ys = [p[1] for p in outline] or [0]
    return {
        "file": str(path),
        "units": "mm",
        "layer_names": {str(k): v for k, v in LAYER_NAMES.items()},
        "layer_alias": {str(k): v for k, v in layer_alias.items()},
        "stack": stack,
        "outline": outline,
        "extent": [min(xs), min(ys), max(xs), max(ys)],
        "origin": [mil_text(board.get("ORIGINX")), mil_text(board.get("ORIGINY"))],
        "nets": nets,
        "components": components,
        "pads": pads,
        "tracks": tracks,
        "arcs": arcs,
        "vias": vias,
        "fills": fills,
        "regions": regions,
        "polygons": polygons,
    }


if __name__ == "__main__":
    import json
    import sys
    r = read_pcb(sys.argv[1])
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in r.items() if k not in ("layer_names",)}, indent=1)[:3000])
