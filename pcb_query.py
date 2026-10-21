#!/usr/bin/env python3
"""
Ask questions of the PCB layout, offline, and draw it with marks on.

  altium_view.py pcb summary                     every board: size, stack, counts, sync
  altium_view.py pcb check [Board]               schematic vs layout, pad by pad
  altium_view.py pcb stack [Board]               the layer stack-up
  altium_view.py pcb part Power/Q1_U_PHASE       placement, pads, nets, neighbours
  altium_view.py pcb net Driver/G_HS_U_PHASE     copper per layer, width, length, vias
  altium_view.py pcb near Power/Q1_U_PHASE 10    what lies within 10 mm
  altium_view.py pcb dist Power/D3_U_PHASE Power/Q1_U_PHASE.1
  altium_view.py pcb mate [C_10]                 board-to-board connectors, pins paired by position
  altium_view.py pcb rules [Board] [length]      design rules, net classes, xSignals from the PcbDoc
  altium_view.py pcb length IC1.112 R50.2        pad-to-pad routed length along the copper (+ via barrels)
  altium_view.py pcb xsignals IC1 U1 --ref FMC_SDCLK --tol 10 --group data=FMC_D*,FMC_NBL*
                                                 every signal SRC->DST, through series parts, length-matched
  altium_view.py pcb clearance Power --max 3     same-layer copper gaps between different nets
  altium_view.py pcb render Driver --around U1_U_PHASE:20 --nets G_HS_U_PHASE,S_HS_U_PHASE
                [--layers L3,L4] [--parts A,B] [--mark x,y,text] [--measure A B]
                [--window x0,y0,x1,y1] [--labels] [--bottom] [-o file.png]

A designator is unique within one board, so name the board when it matters:
`Power/Q1_U_PHASE`, or pass the board as a separate word. Coordinates are mm
in Altium's absolute frame (what the PCB editor shows).

The layout is read straight from the .PcbDoc on every call (a cached parse is
reused while the file is unchanged). Unsaved edits in Altium are not visible.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import altium_pcb
import discover
import project
import query
from project import P

PCB_CACHE_VERSION = 7


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def pcb_paths(board_spec: dict) -> list[Path]:
    prj = Path(board_spec["project"])
    found = [p for p in discover.ini_documents(prj, (".pcbdoc",)) if p.exists()]
    return found or sorted(prj.parent.glob("*.PcbDoc"))


def load_pcb(path: Path) -> dict:
    cache_dir = P.out / "pcb"
    cache_dir.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    stamp = {"v": PCB_CACHE_VERSION, "size": st.st_size, "mtime": st.st_mtime}
    cache = cache_dir / (path.stem + ".json")
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("_stamp") == stamp:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    data = altium_pcb.read_pcb(path)
    data["_stamp"] = stamp
    data["_read"] = datetime.now().isoformat(timespec="seconds")
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(cache)
    return data


class Boards:
    """Schematic model + the layout of every board that has one."""

    def __init__(self):
        self.model = query.load()
        self.specs = {b["key"]: b for b in P.spec["boards"]}
        self.items = []                     # (schematic board, pcb dict)
        for b in self.model["boards"]:
            spec = self.specs.get(b["key"])
            paths = pcb_paths(spec) if spec else []
            if not paths:
                continue
            pcb = load_pcb(paths[0])
            index(pcb)
            self.items.append((b, pcb))

    def pick(self, word: str | None):
        if not word:
            return None
        w = word.lower()
        for b, pcb in self.items:
            if w in (b["title"].lower(), b["key"].lower()) or \
               b["title"].lower().startswith(w) or b["title"].lower().split()[0] == w:
                return b, pcb
        return None

    def resolve(self, ref: str, board_word: str | None = None):
        """'Board/NAME' or NAME (+ optional board word) -> [(board, pcb, name)]."""
        if "/" in ref:
            bw, name = ref.split("/", 1)
            hit = self.pick(bw)
            return [(hit[0], hit[1], name)] if hit else []
        if board_word:
            hit = self.pick(board_word)
            return [(hit[0], hit[1], ref)] if hit else []
        return [(b, pcb, ref) for b, pcb in self.items]


def index(pcb: dict) -> None:
    pcb["_comp"] = {c["designator"]: c for c in pcb["components"]}
    pads_by = defaultdict(list)
    for p in pcb["pads"]:
        pads_by[p["component"]].append(p)
    pcb["_pads"] = pads_by
    alias = {int(k): v for k, v in pcb["layer_alias"].items()}
    pcb["_alias"] = alias


def lname(pcb: dict, layer: int) -> str:
    a = pcb["_alias"].get(layer)
    base = altium_pcb.LAYER_NAMES.get(layer, str(layer))
    return f"{a} ({base})" if a else base


def short_lname(pcb: dict, layer: int) -> str:
    return pcb["_alias"].get(layer) or altium_pcb.LAYER_NAMES.get(layer, str(layer))


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def pad_corners(p: dict) -> list[tuple[float, float]]:
    hx, hy = p["sx"] / 2, p["sy"] / 2
    a = math.radians(p["rotation"])
    ca, sa = math.cos(a), math.sin(a)
    return [(p["x"] + dx * ca - dy * sa, p["y"] + dx * sa + dy * ca)
            for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]


def comp_bbox(pcb: dict, des: str):
    pads = pcb["_pads"].get(des, [])
    pts = [c for p in pads for c in pad_corners(p)]
    c = pcb["_comp"].get(des)
    if not pts:
        return (c["x"] - 0.5, c["y"] - 0.5, c["x"] + 0.5, c["y"] + 0.5) if c else None
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def point_of(pcb: dict, ref: str):
    """'DES' -> centre + bbox, 'DES.PAD' -> pad centre + pad bbox."""
    if "." in ref:
        des, pad = ref.rsplit(".", 1)
        for p in pcb["_pads"].get(des, []):
            if p["name"] == pad:
                cs = pad_corners(p)
                return (p["x"], p["y"]), (min(c[0] for c in cs), min(c[1] for c in cs),
                                          max(c[0] for c in cs), max(c[1] for c in cs))
        return None
    c = pcb["_comp"].get(ref)
    if not c:
        return None
    return (c["x"], c["y"]), comp_bbox(pcb, ref)


def track_len(t: dict) -> float:
    return math.hypot(t["x2"] - t["x1"], t["y2"] - t["y1"])


def arc_len(a: dict) -> float:
    sweep = (a["end"] - a["start"]) % 360 or 360
    return math.radians(sweep) * a["r"]


# --------------------------------------------------------------------------
# copper connectivity (raster)
# --------------------------------------------------------------------------

def copper_layers(pcb: dict) -> list[int]:
    return [s["layer"] for s in pcb["stack"] if s.get("kind") == "copper"]


def net_copper(pcb: dict, net: str, res: float = 0.05, window=None, keep: bool = False) -> dict:
    """
    Rasterise every piece of copper carrying `net` and work out what is
    actually joined to what.

    Each copper layer is drawn on its own grid (pours with their cut-outs
    removed, tracks and arcs at their width with round ends, pads, vias), the
    grid is split into touching islands, and vias and through-hole pads then
    join islands on different layers. Pours drawn as separate polygons but
    overlapping or touching therefore count as one piece of copper, exactly as
    they would be etched. The grid step bounds the accuracy: gaps narrower than
    about two steps can read as joined.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage

    layers = copper_layers(pcb)
    order = {l: i for i, l in enumerate(layers)}

    regs = [r for r in pcb["regions"] if r["net"] == net and r["layer"] in order
            and not r["keepout"] and len(r["points"]) >= 3]
    trks = [t for t in pcb["tracks"] if t["net"] == net and t["layer"] in order and not t["keepout"]]
    arcs = [a for a in pcb["arcs"] if a["net"] == net and a["layer"] in order and not a["keepout"]]
    pads = [p for p in pcb["pads"] if p["net"] == net and (p["layer"] in order or p["layer"] == 74)]
    vias = [v for v in pcb["vias"] if v["net"] == net]

    # Internal planes are negative: copper everywhere inside the split-plane
    # outline (or the board outline), minus the void lines drawn on the layer
    # and the anti-pads around other nets' vias and through-hole pads.
    plane_nets = {int(k): v for k, v in (pcb.get("plane_nets") or {}).items()}
    planes = []                                         # (layer, outline points)
    for l in layers:
        if not (39 <= l <= 54):
            continue
        splits = [pg["points"] for pg in pcb["polygons"]
                  if pg["layer"] == l and pg["net"] == net and len(pg["points"]) >= 3
                  and "split" in (pg.get("type") or "").lower()]
        if splits:
            planes += [(l, pts) for pts in splits]
        elif plane_nets.get(l) == net and len(pcb.get("outline") or []) >= 3:
            planes.append((l, pcb["outline"]))

    xs, ys = [], []
    for _, pts in planes:
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    for r in regs:
        xs += [p[0] for p in r["points"]]
        ys += [p[1] for p in r["points"]]
    for t in trks:
        xs += [t["x1"], t["x2"]]
        ys += [t["y1"], t["y2"]]
    for a in arcs:
        xs += [a["cx"] - a["r"], a["cx"] + a["r"]]
        ys += [a["cy"] - a["r"], a["cy"] + a["r"]]
    for p in pads:
        for cx, cy in pad_corners(p):
            xs.append(cx)
            ys.append(cy)
    for v in vias:
        xs.append(v["x"])
        ys.append(v["y"])
    if not xs:
        return {"layers": {}, "pieces": [], "res": res}
    x0, y0 = min(xs) - 2, min(ys) - 2
    x1, y1 = max(xs) + 2, max(ys) + 2
    if window:
        x0, y0 = max(x0, window[0]), max(y0, window[1])
        x1, y1 = min(x1, window[2]), min(y1, window[3])
        if x1 <= x0 or y1 <= y0:
            return {"layers": {}, "pieces": [], "res": res}
    W, H = int((x1 - x0) / res) + 1, int((y1 - y0) / res) + 1

    def px(x, y):
        return (x - x0) / res, (y1 - y) / res

    grids = {l: np.zeros((H, W), dtype=bool) for l in layers}

    for r in regs:
        pts = [px(x, y) for x, y in r["points"]]
        bx0 = int(max(0, min(p[0] for p in pts)))
        by0 = int(max(0, min(p[1] for p in pts)))
        bx1 = int(min(W - 1, max(p[0] for p in pts)))
        by1 = int(min(H - 1, max(p[1] for p in pts)))
        if bx1 < bx0 or by1 < by0:
            continue                                    # outside the window
        img = Image.new("1", (bx1 - bx0 + 1, by1 - by0 + 1), 0)
        d = ImageDraw.Draw(img)
        d.polygon([(x - bx0, y - by0) for x, y in pts], fill=1)
        for h in r["holes"]:
            if len(h) >= 3:
                d.polygon([(px(x, y)[0] - bx0, px(x, y)[1] - by0) for x, y in h], fill=0)
        grids[r["layer"]][by0:by1 + 1, bx0:bx1 + 1] |= np.array(img, dtype=bool)

    PLANE_CLEARANCE = 0.25                              # anti-pad margin, mm (Altium default ~0.5 dia)
    for l in sorted({l for l, _ in planes}):
        img = Image.new("1", (W, H), 0)
        d = ImageDraw.Draw(img)
        for pl, pts in planes:
            if pl == l:
                d.polygon([px(x, y) for x, y in pts], fill=1)
        for t in pcb["tracks"]:
            if t["layer"] == l and t["net"] != net:
                wpx = max(1, int(round(t["width"] / res)))
                d.line([px(t["x1"], t["y1"]), px(t["x2"], t["y2"])], fill=0, width=wpx)
        for a in pcb["arcs"]:
            if a["layer"] == l and a["net"] != net:
                sweep = (a["end"] - a["start"]) % 360 or 360
                n = max(4, int(sweep / 5))
                d.line([px(a["cx"] + a["r"] * math.cos(math.radians(a["start"] + sweep * k / n)),
                           a["cy"] + a["r"] * math.sin(math.radians(a["start"] + sweep * k / n)))
                        for k in range(n + 1)], fill=0, width=max(1, int(round(a["width"] / res))))
        holes = [(v["x"], v["y"], v["diameter"]) for v in pcb["vias"] if v["net"] != net]
        holes += [(q["x"], q["y"], max(q["sx"], q["sy"])) for q in pcb["pads"]
                  if q["layer"] == 74 and q["net"] != net and q["hole"] > 0.01]
        for x, y, dia in holes:
            cx, cy = px(x, y)
            rr = (dia / 2 + PLANE_CLEARANCE) / res
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=0)
        grids[l] |= np.array(img, dtype=bool)

    imgs, draws = {}, {}

    def canvas(l):
        if l not in imgs:
            imgs[l] = Image.new("1", (W, H), 0)
            draws[l] = ImageDraw.Draw(imgs[l])
        return draws[l]

    def stroke(l, pts, width):
        d = canvas(l)
        wpx = max(1, int(round(width / res)))
        d.line(pts, fill=1, width=wpx)
        rr = wpx / 2
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=1)

    for t in trks:
        stroke(t["layer"], [px(t["x1"], t["y1"]), px(t["x2"], t["y2"])], t["width"])
    for a in arcs:
        sweep = (a["end"] - a["start"]) % 360 or 360
        n = max(4, int(sweep / 5))
        pts = [px(a["cx"] + a["r"] * math.cos(math.radians(a["start"] + sweep * k / n)),
                  a["cy"] + a["r"] * math.sin(math.radians(a["start"] + sweep * k / n)))
               for k in range(n + 1)]
        stroke(a["layer"], pts, a["width"])

    def pad_layers(p):
        return layers if p["layer"] == 74 else [p["layer"]]

    for p in pads:
        for l in pad_layers(p):
            d = canvas(l)
            if p["shape"] == "round" and abs(p["sx"] - p["sy"]) < 1e-3:
                cx, cy = px(p["x"], p["y"])
                rr = p["sx"] / 2 / res
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=1)
            else:
                d.polygon([px(x, y) for x, y in pad_corners(p)], fill=1)

    def via_layers(v):
        ia = order.get(v["from"], 0)
        ib = order.get(v["to"], len(layers) - 1)
        lo, hi = min(ia, ib), max(ia, ib)
        return layers[lo:hi + 1]

    for v in vias:
        cx, cy = px(v["x"], v["y"])
        rr = v["diameter"] / 2 / res
        for l in via_layers(v):
            canvas(l).ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=1)

    for l, img in imgs.items():
        grids[l] |= np.array(img, dtype=bool)

    labels, counts, areas = {}, {}, {}
    st = np.ones((3, 3), dtype=bool)
    for l in layers:
        lab, n = ndimage.label(grids[l], structure=st)
        labels[l], counts[l] = lab, n
        areas[l] = float(grids[l].sum()) * res * res

    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def label_at(l, x, y):
        cx, cy = px(x, y)
        i, j = int(round(cy)), int(round(cx))
        if 0 <= i < H and 0 <= j < W:
            v = int(labels[l][i, j])
            return (l, v) if v else None
        return None

    for l in layers:
        for k in range(1, counts[l] + 1):
            find((l, k))
    for v in vias:
        hits = [h for h in (label_at(l, v["x"], v["y"]) for l in via_layers(v)) if h]
        for h in hits[1:]:
            union(hits[0], h)
    for p in pads:
        if p["layer"] == 74 and p["hole"] > 0.01:
            hits = [h for h in (label_at(l, p["x"], p["y"]) for l in layers) if h]
            for h in hits[1:]:
                union(hits[0], h)

    groups = defaultdict(lambda: {"pads": [], "layers": set()})
    for l in layers:
        for k in range(1, counts[l] + 1):
            groups[find((l, k))]["layers"].add(l)
    for p in pads:
        for l in pad_layers(p):
            h = label_at(l, p["x"], p["y"])
            if h:
                groups[find(h)]["pads"].append(f"{p['component']}.{p['name']}")
                break
    pieces = []
    for g in groups.values():
        g["pads"] = sorted(set(g["pads"]))
        pieces.append(g)
    pieces.sort(key=lambda g: -len(g["pads"]))
    out = {"layers": {l: {"area": areas[l], "islands": counts[l]} for l in layers if counts[l]},
           "pieces": pieces, "res": res}
    if keep:
        out.update(grids=grids, frame=(x0, y1, res, W, H), layers_order=layers,
                   vias=vias, pads=pads, via_layers=via_layers, pad_layers=pad_layers)
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def sync_report(b: dict, pcb: dict) -> dict:
    """Compare pad nets against the schematic, by connectivity not by name."""
    sch_pin = {}
    for name, rec in b["nets"].items():
        for nd in rec["nodes"]:
            sch_pin[(nd["designator"], str(nd["pin"]))] = name
    sch_parts = set(b["components"])
    pcb_parts = {c["designator"] for c in pcb["components"]}
    pcb_pin = {(p["component"], p["name"]): p["net"]
               for p in pcb["pads"] if p["component"] and p["name"]}

    # each schematic net should map onto exactly one layout net and back
    s2p, p2s = defaultdict(set), defaultdict(set)
    for key, snet in sch_pin.items():
        if key in pcb_pin:
            pnet = pcb_pin[key]
            s2p[snet].add(pnet)
            if pnet:
                p2s[pnet].add(snet)
    split = {s: sorted(map(str, v)) for s, v in s2p.items() if len(v) > 1}
    merged = {p: sorted(v) for p, v in p2s.items() if len(v) > 1}
    unnetted = sorted(f"{d}.{n}" for (d, n), pn in pcb_pin.items()
                      if pn is None and (d, n) in sch_pin
                      and b["nets"][sch_pin[(d, n)]]["node_count"] > 1)
    renamed = {s: next(iter(v)) for s, v in s2p.items()
               if len(v) == 1 and None not in v and next(iter(v)) != s}
    ex0, ey0, ex1, ey1 = pcb["extent"]
    off_board = sorted(c["designator"] for c in pcb["components"]
                       if c["x"] is not None and not (ex0 <= c["x"] <= ex1 and ey0 <= c["y"] <= ey1))
    return {
        "off_board": off_board,
        "missing_in_pcb": sorted(sch_parts - pcb_parts),
        "extra_in_pcb": sorted(pcb_parts - sch_parts),
        "split": split, "merged": merged, "unnetted": unnetted, "renamed": renamed,
    }


def cmd_summary(B: Boards, args) -> None:
    for b, pcb in B.items:
        x0, y0, x1, y1 = pcb["extent"]
        cu = [s for s in pcb["stack"] if s.get("kind") == "copper"]
        r = sync_report(b, pcb)
        bad = (len(r["missing_in_pcb"]) + len(r["off_board"]) + len(r["extra_in_pcb"]) + len(r["split"])
               + len(r["merged"]) + len(r["unnetted"]))
        print(f"{b['title']:<14} {x1 - x0:6.1f} x {y1 - y0:5.1f} mm   "
              f"{len(cu)} copper layers   {len(pcb['components'])} parts  "
              f"{len(pcb['tracks'])} tracks  {len(pcb['vias'])} vias  "
              f"{len(pcb['polygons'])} pours   "
              f"{'IN SYNC with schematic' if not bad else f'{bad} schematic/layout difference(s) -- pcb check'}")
        print(f"   {Path(pcb['file']).name}   read {pcb.get('_read', '?')}")


def cmd_stack(B: Boards, args) -> None:
    for b, pcb in pick_boards(B, args):
        print(f"{b['title']}")
        for s in pcb["stack"]:
            if s["kind"] == "copper":
                oz = (s.get("copper_mm") or 0) / 0.0348
                print(f"   {('L' + str(s['index'])):<4} {s['name']:<18} copper "
                      f"{(s.get('copper_mm') or 0) * 1000:5.0f} um (~{oz:.1f} oz)")
            else:
                print(f"        {s['name']:<18} {s.get('material') or 'dielectric':<10} "
                      f"{(s.get('height_mm') or 0):.3f} mm  er {s.get('er') or 0:.2f}")


def pick_boards(B: Boards, args):
    if args:
        hit = B.pick(args[0])
        if hit:
            return [hit]
    return B.items


def cmd_check(B: Boards, args) -> None:
    for b, pcb in pick_boards(B, args):
        r = sync_report(b, pcb)
        print(f"{b['title']}  ({Path(pcb['file']).name})")
        clean = True
        for key, label in (("missing_in_pcb", "in schematic, not on the PCB yet (ECO not run)"),
                           ("off_board", "on the PCB but parked outside the board outline"),
                           ("extra_in_pcb", "on the PCB, not in the schematic"),
                           ("unnetted", "pad has no net although the schematic connects it")):
            if r[key]:
                clean = False
                print(f"   !! {len(r[key])} {label}: {', '.join(d or '(unnamed)' for d in r[key][:30])}"
                      f"{' ...' if len(r[key]) > 30 else ''}")
        for s, v in r["split"].items():
            clean = False
            print(f"   !! schematic net {s} is on {len(v)} different PCB nets: {', '.join(v)}")
        for p, v in r["merged"].items():
            clean = False
            print(f"   !! PCB net {p} joins schematic nets {', '.join(v)}")
        if r["renamed"]:
            print(f"   (info) {len(r['renamed'])} net(s) carry a different name on the PCB, "
                  f"same connectivity")
        if clean:
            print("   in sync: every part placed, every pad on the net the schematic gives it")
        print()


def find_parts(B: Boards, ref: str, board_word=None):
    out = []
    for b, pcb, name in B.resolve(ref, board_word):
        if name.split(".")[0] in pcb["_comp"]:
            out.append((b, pcb, name))
    return out


def cmd_part(B: Boards, args) -> None:
    if not args:
        sys.exit("pcb part needs a designator, e.g. pcb part Power/Q1_U_PHASE")
    hits = find_parts(B, args[0], args[1] if len(args) > 1 else None)
    if not hits:
        sys.exit(f"{args[0]} is not placed on any PCB")
    for b, pcb, des in hits:
        c = pcb["_comp"][des]
        bb = comp_bbox(pcb, des)
        print(f"{des}   [{b['title']}]   {c['footprint']}   ({c['lib_ref']})")
        print(f"   at ({c['x']:.2f}, {c['y']:.2f}) mm   rot {c['rotation']:.0f} deg   "
              f"{c['side']} side   pads span {bb[2] - bb[0]:.2f} x {bb[3] - bb[1]:.2f} mm")
        print("   pads:")
        for p in sorted(pcb["_pads"].get(des, []), key=lambda p: (len(p["name"]), p["name"])):
            hole = f" hole {p['hole']:.2f}" if p["hole"] > 0.01 else ""
            print(f"     {p['name']:<5} {str(p['net'] or '-'):<26} ({p['x']:.2f}, {p['y']:.2f}) "
                  f"{p['sx']:.2f}x{p['sy']:.2f} {p['shape']:<9} {short_lname(pcb, p['layer'])}{hole}")
        near = neighbours(pcb, des, 5.0)
        if near:
            print("   within 5 mm (edge to edge): " +
                  ", ".join(f"{d} {g:.1f}" for d, g in near[:14]))


def neighbours(pcb: dict, des: str, radius: float):
    bb = comp_bbox(pcb, des)
    out = []
    for other in pcb["_comp"]:
        if other == des:
            continue
        ob = comp_bbox(pcb, other)
        if ob is None:
            continue
        g = bbox_gap(bb, ob)
        if g <= radius:
            out.append((other, g))
    return sorted(out, key=lambda t: t[1])


def cmd_near(B: Boards, args) -> None:
    if not args:
        sys.exit("pcb near needs a designator and optionally a radius in mm")
    radius = float(args[1]) if len(args) > 1 and args[1].replace(".", "").isdigit() else 5.0
    for b, pcb, des in find_parts(B, args[0]):
        print(f"{des} [{b['title']}] -- parts within {radius} mm, edge to edge")
        for d, g in neighbours(pcb, des, radius):
            c = pcb["_comp"][d]
            print(f"   {g:5.2f} mm  {d:<18} {c['lib_ref'] or c['footprint']}  ({c['side']})")


def cmd_dist(B: Boards, args) -> None:
    if len(args) < 2:
        sys.exit("pcb dist needs two references: DES or DES.PAD, e.g. Power/D3_U_PHASE Power/Q1_U_PHASE.1")
    a_hits = B.resolve(args[0])
    for b, pcb, a in a_hits:
        bref = args[1].split("/", 1)[1] if "/" in args[1] else args[1]
        pa, pb = point_of(pcb, a), point_of(pcb, bref)
        if not (pa and pb):
            continue
        (ax, ay), abb = pa
        (bx, by), bbb = pb
        print(f"{b['title']}: {a} -> {bref}   centre {math.hypot(ax - bx, ay - by):.2f} mm   "
              f"edge {bbox_gap(abb, bbb):.2f} mm")


def cmd_net(B: Boards, args) -> None:
    if not args:
        sys.exit("pcb net needs a net name, e.g. pcb net Driver/G_HS_U_PHASE")
    shown = 0
    for b, pcb, net in B.resolve(args[0], args[1] if len(args) > 1 else None):
        if net not in pcb["nets"]:
            continue
        shown += 1
        print(f"{net}   [{b['title']}]")
        per = defaultdict(lambda: [0.0, set(), 0])
        for t in pcb["tracks"]:
            if t["net"] == net and altium_pcb.is_copper(t["layer"]):
                r = per[t["layer"]]
                r[0] += track_len(t)
                r[1].add(round(t["width"], 3))
                r[2] += 1
        for a in pcb["arcs"]:
            if a["net"] == net and altium_pcb.is_copper(a["layer"]):
                r = per[a["layer"]]
                r[0] += arc_len(a)
                r[1].add(round(a["width"], 3))
        if per:
            print("   tracks:")
            for layer in sorted(per, key=lambda l: (l != 1, l == 32, l)):
                length, widths, n = per[layer]
                print(f"     {lname(pcb, layer):<26} {length:7.1f} mm in {n} segment(s), "
                      f"width {', '.join(f'{w:.3f}' for w in sorted(widths))} mm")
        vias = [v for v in pcb["vias"] if v["net"] == net]
        if vias:
            print(f"   vias: {len(vias)}  ({vias[0]['diameter']:.2f}/{vias[0]['hole']:.2f} mm)")
        defs = defaultdict(int)
        for pg in pcb["polygons"]:
            if pg["net"] == net:
                defs[pg["layer"]] += 1
        cu = net_copper(pcb, net)
        if cu["layers"]:
            print("   copper per layer (tracks + pours + pads + vias merged, cut-outs removed):")
            for l, rec in cu["layers"].items():
                pd = f", drawn as {defs[l]} pour(s)" if defs.get(l) else ""
                print(f"     {lname(pcb, l):<26} {rec['area']:8.1f} mm2 in "
                      f"{rec['islands']} separate island(s){pd}")
        pieces = [g for g in cu["pieces"] if g["pads"]]
        stray = [g for g in cu["pieces"] if not g["pads"]]
        if len(pieces) <= 1:
            print("   connectivity: every pad of this net is joined by copper"
                  + (f"  ({len(stray)} copper island(s) touch no pad)" if stray else ""))
        else:
            print(f"   !! connectivity: the copper forms {len(pieces)} separate pieces -- "
                  f"not fully routed (or joined only through something this tool does not see):")
            cl = copper_layers(pcb)
            for k, g in enumerate(pieces, 1):
                ls = ", ".join(short_lname(pcb, l) for l in sorted(g["layers"], key=cl.index))
                print(f"     piece {k} [{ls}]: {' '.join(g['pads'][:16])}"
                      f"{' ...' if len(g['pads']) > 16 else ''}")
            if stray:
                print(f"     plus {len(stray)} copper island(s) that touch no pad")
        pads = [p for p in pcb["pads"] if p["net"] == net]
        print(f"   pads ({len(pads)}): " + " ".join(
            f"{p['component']}.{p['name']}" for p in sorted(pads, key=lambda p: (str(p['component']), p['name']))))
    if not shown:
        sys.exit(f"no net {args[0]!r} on any PCB")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

LAYER_COLOURS = ["#d62728", "#ff7f0e", "#2ca02c", "#1f77b4", "#9467bd", "#8c564b"]
HILITE = ["#ff00ff", "#00c8ff", "#ffd400", "#00e070", "#ff6a00", "#7a5cff"]


def parse_render_args(args):
    opts = {"layers": None, "nets": [], "parts": [], "marks": [], "measure": [],
            "window": None, "around": None, "labels": False, "bottom": False,
            "out": None, "board": None}
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--layers":
            opts["layers"] = [s.strip() for s in nxt.split(",")]
            i += 2
        elif a == "--nets":
            opts["nets"] += [s.strip() for s in nxt.split(",") if s.strip()]
            i += 2
        elif a == "--parts":
            opts["parts"] += [s.strip() for s in nxt.split(",") if s.strip()]
            i += 2
        elif a == "--mark":
            opts["marks"].append(nxt)
            i += 2
        elif a == "--measure":
            opts["measure"].append((args[i + 1], args[i + 2]))
            i += 3
        elif a == "--window":
            opts["window"] = [float(v) for v in nxt.split(",")]
            i += 2
        elif a == "--around":
            opts["around"] = nxt
            i += 2
        elif a == "--labels":
            opts["labels"] = True
            i += 1
        elif a == "--bottom":
            opts["bottom"] = True
            i += 1
        elif a in ("-o", "--out"):
            opts["out"] = nxt
            i += 2
        else:
            opts["board"] = opts["board"] or a
            i += 1
    return opts


def cmd_render(B: Boards, args) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PatchCollection
    from matplotlib.patches import Circle, Polygon as MPoly

    o = parse_render_args(args)
    board_word = o["board"]
    if not board_word and o["around"] and "/" in o["around"]:
        board_word = o["around"].split("/", 1)[0]
    hit = B.pick(board_word) if board_word else (B.items[0] if len(B.items) == 1 else None)
    if not hit:
        sys.exit("render needs a board: " + ", ".join(b["title"] for b, _ in B.items))
    b, pcb = hit

    cu_layers = [s["layer"] for s in pcb["stack"] if s.get("kind") == "copper"]
    if o["layers"]:
        want = []
        for w in o["layers"]:
            w2 = w.lower()
            for s in pcb["stack"]:
                if s.get("kind") == "copper" and w2 in (f"l{s['index']}", s["name"].lower()):
                    want.append(s["layer"])
            if w2 in ("top",):
                want.append(1)
            if w2 in ("bottom", "bot"):
                want.append(32)
        layers = [l for l in cu_layers if l in want]
    else:
        layers = cu_layers
    colour = {l: LAYER_COLOURS[i % len(LAYER_COLOURS)] for i, l in enumerate(cu_layers)}
    hl = {n: HILITE[i % len(HILITE)] for i, n in enumerate(o["nets"])}
    focus = bool(hl)

    # window
    if o["window"]:
        x0, y0, x1, y1 = o["window"]
    elif o["around"]:
        ref = o["around"].split("/", 1)[-1]
        name, _, rad = ref.partition(":")
        rad = float(rad or 10)
        pt = point_of(pcb, name)
        if not pt:
            sys.exit(f"{name} is not on {b['title']}")
        (cx, cy), bb = pt
        x0, y0, x1, y1 = bb[0] - rad, bb[1] - rad, bb[2] + rad, bb[3] + rad
    else:
        x0, y0, x1, y1 = pcb["extent"]
        pad = 2
        x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad

    w_mm, h_mm = x1 - x0, y1 - y0
    fig_w = 14.0
    fig_h = max(3.0, fig_w * h_mm / max(w_mm, 1e-6))
    if fig_h > 14:
        fig_w, fig_h = fig_w * 14 / fig_h, 14
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=110)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("#101418")
    ax.set_xlim(x1, x0) if o["bottom"] else ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    pt_per_mm = fig_w * 72 / w_mm

    def inside(x, y, m=0):
        return x0 - m <= x <= x1 + m and y0 - m <= y <= y1 + m

    def style(layer, net):
        if focus:
            if net in hl:
                return hl[net], 0.95
            return "#5a6068", 0.35
        return colour.get(layer, "#888888"), 0.55

    # outline
    if pcb["outline"]:
        xs = [p[0] for p in pcb["outline"]] + [pcb["outline"][0][0]]
        ys = [p[1] for p in pcb["outline"]] + [pcb["outline"][0][1]]
        ax.plot(xs, ys, color="#e0e0e0", lw=1.0, zorder=1)

    # draw layers bottom-most first so the top layer sits on top
    order = sorted(layers, key=lambda l: cu_layers.index(l), reverse=not o["bottom"])
    z = 2
    for layer in order:
        polys, cols, alphas = [], [], []
        for r in pcb["regions"]:
            if r["layer"] != layer or r["keepout"] or len(r["points"]) < 3:
                continue
            if not any(inside(px, py, 20) for px, py in r["points"][:: max(1, len(r["points"]) // 20)]):
                continue
            c, a = style(layer, r["net"])
            ax.add_patch(MPoly(r["points"], closed=True, facecolor=c, edgecolor="none",
                               alpha=a * 0.6, zorder=z))
        segs, widths, scol = [], [], []
        for t in pcb["tracks"]:
            if t["layer"] != layer or t["keepout"]:
                continue
            if not (inside(t["x1"], t["y1"], 5) or inside(t["x2"], t["y2"], 5)):
                continue
            c, a = style(layer, t["net"])
            segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
            widths.append(max(0.3, t["width"] * pt_per_mm))
            scol.append(matplotlib.colors.to_rgba(c, a))
        for a_ in pcb["arcs"]:
            if a_["layer"] != layer or a_["keepout"] or not inside(a_["cx"], a_["cy"], 5):
                continue
            sweep = (a_["end"] - a_["start"]) % 360 or 360
            n = max(4, int(sweep / 10))
            pts = [(a_["cx"] + a_["r"] * math.cos(math.radians(a_["start"] + sweep * k / n)),
                    a_["cy"] + a_["r"] * math.sin(math.radians(a_["start"] + sweep * k / n)))
                   for k in range(n + 1)]
            c, al = style(layer, a_["net"])
            for p, q in zip(pts, pts[1:]):
                segs.append([p, q])
                widths.append(max(0.3, a_["width"] * pt_per_mm))
                scol.append(matplotlib.colors.to_rgba(c, al))
        if segs:
            ax.add_collection(LineCollection(segs, linewidths=widths, colors=scol,
                                             capstyle="round", zorder=z + 1))
        z += 3

    # pads (on visible layers or through-hole), then vias
    for p in pcb["pads"]:
        if not inside(p["x"], p["y"], 3):
            continue
        if p["layer"] != 74 and p["layer"] not in layers:
            continue
        c, a = style(p["layer"] if p["layer"] != 74 else layers[0], p["net"])
        if p["shape"] == "round" and abs(p["sx"] - p["sy"]) < 1e-3:
            patch = Circle((p["x"], p["y"]), p["sx"] / 2)
        else:
            patch = MPoly(pad_corners(p), closed=True)
        patch.set_facecolor(c)
        patch.set_alpha(min(1.0, a + 0.25))
        patch.set_edgecolor("white" if p["net"] in hl else "none")
        patch.set_linewidth(0.6)
        patch.set_zorder(z)
        ax.add_patch(patch)
        if p["hole"] > 0.01:
            ax.add_patch(Circle((p["x"], p["y"]), p["hole"] / 2, color="#101418", zorder=z + 1))
    z += 2
    for v in pcb["vias"]:
        if not inside(v["x"], v["y"], 2):
            continue
        c, a = style(layers[0] if layers else 1, v["net"])
        ax.add_patch(Circle((v["x"], v["y"]), v["diameter"] / 2, facecolor=c,
                            alpha=min(1.0, a + 0.2), edgecolor="none", zorder=z))
        ax.add_patch(Circle((v["x"], v["y"]), v["hole"] / 2, color="#101418", zorder=z + 1))
    z += 2

    # parts: highlighted boxes, optional labels
    fs = max(5, min(9, 900 / max(w_mm, 1)))
    for des, c in pcb["_comp"].items():
        bb = comp_bbox(pcb, des)
        if not bb or not inside((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2):
            continue
        marked = des in o["parts"]
        if marked:
            ax.add_patch(MPoly([(bb[0] - .3, bb[1] - .3), (bb[2] + .3, bb[1] - .3),
                                (bb[2] + .3, bb[3] + .3), (bb[0] - .3, bb[3] + .3)],
                               closed=True, fill=False, edgecolor="#ffffff", lw=1.6, zorder=z))
        if marked or o["labels"]:
            ax.text((bb[0] + bb[2]) / 2, bb[3] + 0.4, des, color="#ffffff" if marked else "#c8c8c8",
                    fontsize=fs if marked else fs * 0.75, ha="center", va="bottom", zorder=z + 1,
                    fontweight="bold" if marked else "normal")

    # measurements and free marks
    for ra, rb in o["measure"]:
        pa = point_of(pcb, ra.split("/", 1)[-1])
        pb = point_of(pcb, rb.split("/", 1)[-1])
        if not (pa and pb):
            continue
        (ax_, ay_), _ = pa
        (bx_, by_), _ = pb
        d = math.hypot(ax_ - bx_, ay_ - by_)
        ax.annotate("", xy=(bx_, by_), xytext=(ax_, ay_),
                    arrowprops=dict(arrowstyle="<->", color="#ffff66", lw=1.4), zorder=z + 2)
        ax.text((ax_ + bx_) / 2, (ay_ + by_) / 2, f"{d:.1f} mm", color="#ffff66",
                fontsize=fs, ha="center", va="bottom", zorder=z + 3, fontweight="bold")
    for m in o["marks"]:
        parts = m.split(",", 2)
        mx, my = float(parts[0]), float(parts[1])
        ax.plot([mx], [my], marker="x", color="#ff4040", ms=10, mew=2, zorder=z + 2)
        if len(parts) > 2:
            ax.text(mx, my, "  " + parts[2], color="#ff4040", fontsize=fs, zorder=z + 3,
                    fontweight="bold")

    # legend
    legend = [f"{b['title']}  view from {'BOTTOM (mirrored)' if o['bottom'] else 'top'}  "
              f"layers: {', '.join(short_lname(pcb, l) for l in layers)}"]
    for n, c in hl.items():
        legend.append(f"{n}")
    for k, line in enumerate(legend):
        ax.text(0.005, 0.995 - k * 0.03, line, transform=ax.transAxes, ha="left", va="top",
                color=list(hl.values())[k - 1] if k else "#ffffff", fontsize=9,
                bbox=dict(facecolor="#101418", alpha=0.7, edgecolor="none"), zorder=99)
    # scale bar
    sb = 10 if w_mm > 40 else (5 if w_mm > 15 else 1)
    sx0 = x0 + w_mm * 0.03 if not o["bottom"] else x1 - w_mm * 0.03
    sy0 = y0 + h_mm * 0.04
    ax.plot([sx0, sx0 + (sb if not o["bottom"] else -sb)], [sy0, sy0], color="white", lw=2, zorder=99)
    ax.text(sx0, sy0 + h_mm * 0.01, f"{sb} mm", color="white", fontsize=8, zorder=99)

    out = Path(o["out"]) if o["out"] else (
        P.out / "pcb" / f"render_{b['key']}_{datetime.now().strftime('%H%M%S')}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=ax.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}")
    print(f"   window x {x0:.1f}..{x1:.1f}  y {y0:.1f}..{y1:.1f} mm   layers "
          f"{', '.join(short_lname(pcb, l) for l in layers)}")


# --------------------------------------------------------------------------
# copper path length between two pads
# --------------------------------------------------------------------------

def layer_gaps(pcb: dict) -> dict:
    """Vertical distance (mm) between consecutive copper layers."""
    gaps, last, acc = {}, None, 0.0
    for s in pcb["stack"]:
        if s.get("kind") == "copper":
            if last is not None:
                gaps[(last, s["layer"])] = gaps[(s["layer"], last)] = acc
            last, acc = s["layer"], 0.0
        else:
            acc += s.get("height_mm") or 0.0
    return gaps


def find_pad(pcb: dict, ref: str):
    des, _, name = ref.rpartition(".")
    for p in pcb["_pads"].get(des, []):
        if p["name"] == name:
            return p
    return None


def copper_path(pcb: dict, a: dict, b: dict, margin: float = 8.0, res: float = 0.05):
    """
    Shortest route through the copper of one net from pad `a` to pad `b`.

    The net's copper is rasterised in a window around the two pads; each
    copper pixel is a graph node joined to its eight neighbours, and vias /
    through-hole pads join layers with the dielectric thickness as the step.
    Dijkstra from every pixel of pad `a` gives the length along the copper to
    the nearest pixel of pad `b`, and the path's layer changes are counted as
    vias. Returns None when the pads are not joined inside the window.
    """
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra

    net = a["net"]
    if not net or net != b["net"]:
        return None
    win = (min(a["x"], b["x"]) - margin, min(a["y"], b["y"]) - margin,
           max(a["x"], b["x"]) + margin, max(a["y"], b["y"]) + margin)
    cu = net_copper(pcb, net, res=res, window=win, keep=True)
    if "grids" not in cu:
        return None
    x0, y1, res, W, H = cu["frame"]
    layers = cu["layers_order"]
    grids = cu["grids"]

    ids = {}
    base = 0
    for l in layers:
        g = np.full((H, W), -1, dtype=np.int64)
        m = grids[l]
        n = int(m.sum())
        g[m] = np.arange(base, base + n)
        ids[l] = g
        base += n
    if base == 0:
        return None

    rows, cols, wts = [], [], []
    for l in layers:
        g = ids[l]
        for di, dj, w in ((0, 1, 1.0), (1, 0, 1.0), (1, 1, 2 ** 0.5), (1, -1, 2 ** 0.5)):
            if dj >= 0:
                s = g[: H - di, : W - dj]
                d = g[di:, dj:]
            else:
                s = g[: H - di, -dj:]
                d = g[di:, : W + dj]
            ok = (s >= 0) & (d >= 0)
            rows.append(s[ok])
            cols.append(d[ok])
            wts.append(np.full(int(ok.sum()), w * res))

    gaps = layer_gaps(pcb)

    def pix(x, y):
        return int(round((y1 - y) / res)), int(round((x - x0) / res))

    joins = [(v["x"], v["y"], cu["via_layers"](v)) for v in cu["vias"]]
    joins += [(p["x"], p["y"], layers) for p in cu["pads"] if p["layer"] == 74 and p["hole"] > 0.01]
    via_edges = set()
    for x, y, ls in joins:
        i, j = pix(x, y)
        if not (0 <= i < H and 0 <= j < W):
            continue
        for la, lb in zip(ls, ls[1:]):
            na, nb = ids[la][i, j], ids[lb][i, j]
            if na >= 0 and nb >= 0:
                rows.append(np.array([na]))
                cols.append(np.array([nb]))
                wts.append(np.array([gaps.get((la, lb), 0.2) or 0.05]))
                via_edges.add((min(na, nb), max(na, nb)))

    r = np.concatenate(rows)
    c = np.concatenate(cols)
    w = np.concatenate(wts)
    graph = coo_matrix((w, (r, c)), shape=(base, base)).tocsr()

    def pad_nodes(p):
        out = []
        cs = pad_corners(p)
        ii0, jj0 = pix(min(q[0] for q in cs), max(q[1] for q in cs))
        ii1, jj1 = pix(max(q[0] for q in cs), min(q[1] for q in cs))
        for l in (layers if p["layer"] == 74 else [p["layer"]]):
            if l not in ids:
                continue
            sub = ids[l][max(0, ii0):min(H, ii1 + 1), max(0, jj0):min(W, jj1 + 1)]
            out += [int(v) for v in sub[sub >= 0].ravel()]
        return out

    src, dst = pad_nodes(a), pad_nodes(b)
    if not src or not dst:
        return None
    dist, pred, origin = dijkstra(graph, directed=False, indices=src, min_only=True,
                                  return_predecessors=True)
    best = min(dst, key=lambda n: dist[n])
    if not np.isfinite(dist[best]):
        return None
    vias_on_path = 0
    n = best
    while pred[n] >= 0:
        p_ = pred[n]
        if (min(n, p_), max(n, p_)) in via_edges:
            vias_on_path += 1
        n = p_
    straight = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
    return {"length": float(dist[best]), "vias": vias_on_path, "straight": straight}


def two_pin_parts(pcb: dict):
    for des, pads in pcb["_pads"].items():
        named = [p for p in pads if p["name"]]
        if des and len(named) == 2:
            yield des, named


def rate(side_a: dict | None, side_b: dict | None) -> str:
    if not side_a or not side_b:
        return "NOT JOINED"
    loop = side_a["length"] + side_b["length"]
    v = side_a["vias"] + side_b["vias"]
    if loop <= 6 and v <= 2:
        return "good"
    if loop <= 15 and v <= 4:
        return "ok"
    return "CHECK"


def cmd_path(B: Boards, args) -> None:
    if len(args) < 2:
        sys.exit("pcb path needs two pads on one net: pcb path Driver/C13_U_PHASE.1 Driver/U1_U_PHASE.16")
    for b, pcb, ra in B.resolve(args[0]):
        rb = args[1].split("/", 1)[-1]
        pa, pb = find_pad(pcb, ra), find_pad(pcb, rb)
        if not (pa and pb):
            continue
        if pa["net"] != pb["net"]:
            print(f"{b['title']}: {ra} is on {pa['net']}, {rb} on {pb['net']} -- not the same net")
            continue
        r = copper_path(pcb, pa, pb)
        if not r:
            print(f"{b['title']}: {ra} -> {rb} ({pa['net']}): NOT joined by copper within 8 mm of the pads")
            continue
        print(f"{b['title']}: {ra} -> {rb}  ({pa['net']})")
        print(f"   along the copper {r['length']:.2f} mm, {r['vias']} via(s); straight line {r['straight']:.2f} mm")


def cmd_caps(B: Boards, args) -> None:
    """Every two-terminal part both of whose nets reach this IC, with copper paths."""
    if not args:
        sys.exit("pcb caps needs an IC: pcb caps Driver/U1_U_PHASE")
    hits = find_parts(B, args[0], args[1] if len(args) > 1 else None)
    if not hits:
        sys.exit(f"{args[0]} is not placed on any PCB")
    for b, pcb, ic in hits:
        ic_pads = [p for p in pcb["_pads"].get(ic, []) if p["net"]]
        nets = {p["net"] for p in ic_pads}
        rows = []
        for des, (p1, p2) in two_pin_parts(pcb):
            if des == ic or p1["net"] not in nets or p2["net"] not in nets or p1["net"] == p2["net"]:
                continue
            sides = []
            for p in (p1, p2):
                best = None
                for q in ic_pads:
                    if q["net"] != p["net"]:
                        continue
                    r = copper_path(pcb, p, q)
                    if r and (best is None or r["length"] < best[1]["length"]):
                        best = (q, r)
                sides.append((p, best))
            rows.append((des, sides))
        print(f"{ic}  [{b['title']}]  -- two-terminal parts on two of its nets, path along the copper")
        sch = b["components"]
        print(f"   {'part':<14} {'value':<12} {'net':<18} {'pin':<4} {'copper':>7} {'via':>3} {'line':>6}   "
              f"{'net':<18} {'pin':<4} {'copper':>7} {'via':>3}   verdict")
        def key(row):
            s = [x[1][1]["length"] if x[1] else 999 for x in row[1]]
            return sum(s)
        for des, sides in sorted(rows, key=key):
            (pa, ba), (pb, bb) = sides
            def cell(p, best):
                if not best:
                    return f"{str(p['net'])[:18]:<18} {'-':<4} {'--':>7} {'':>3}"
                q, r = best
                return f"{str(p['net'])[:18]:<18} {q['name']:<4} {r['length']:7.2f} {r['vias']:>3}"
            line = f"{ba[1]['straight']:6.1f}" if ba else f"{'-':>6}"
            val = str(sch.get(des, {}).get("comment", ""))[:12]
            print(f"   {des:<14} {val:<12} {cell(pa, ba)} {line}   {cell(pb, bb)}   "
                  f"{rate(ba[1] if ba else None, bb[1] if bb else None)}")
        print("   copper = length along the actual copper (pours, tracks, vias) from the part's pad to the")
        print("   nearest IC pin on that net, in mm; line = straight distance. verdict: good = both sides <= 6 mm and <= 2 vias,")
        print("   ok <= 15 mm / 4 vias, CHECK beyond; NOT JOINED = no copper path within 8 mm.")


# --------------------------------------------------------------------------
# board-wide: routing completeness and copper-to-copper clearance
# --------------------------------------------------------------------------

def cmd_routed(B: Boards, args) -> None:
    """Every net whose pads are not all joined by copper."""
    for b, pcb in pick_boards(B, args):
        nets = sorted({p["net"] for p in pcb["pads"] if p["net"]})
        bad = []
        for n in nets:
            pads = [p for p in pcb["pads"] if p["net"] == n]
            if len(pads) < 2:
                continue
            cu = net_copper(pcb, n, res=0.1)
            pieces = [g for g in cu["pieces"] if g["pads"]]
            if len(pieces) > 1:
                bad.append((n, pieces))
        print(f"{b['title']}: {len(nets)} nets, {len(bad)} not fully joined by copper")
        for n, pieces in bad:
            small = sorted(pieces, key=lambda g: len(g["pads"]))[:-1]
            loose = [p for g in small for p in g["pads"]]
            print(f"   {n:<28} {len(pieces)} pieces; cut off: {' '.join(loose[:10])}"
                  f"{' ...' if len(loose) > 10 else ''}")
        print()


def layer_net_image(pcb: dict, layer: int, names: dict, frame, res: float):
    """One copper layer drawn with a net id per pixel (0 = no copper, -1 = two nets)."""
    import numpy as np
    from PIL import Image, ImageDraw
    x0, y1, W, H = frame
    img = np.zeros((H, W), dtype=np.int32)

    def px(x, y):
        return (x - x0) / res, (y1 - y) / res

    def paint(nid, draw_fn):
        m = Image.new("1", (W, H), 0)
        draw_fn(ImageDraw.Draw(m))
        mask = np.array(m, dtype=bool)
        clash = mask & (img != 0) & (img != nid)
        img[mask & (img == 0)] = nid
        img[clash] = -1

    by_net = defaultdict(list)
    for r in pcb["regions"]:
        if r["layer"] == layer and not r["keepout"] and r["net"] and len(r["points"]) >= 3:
            by_net[r["net"]].append(("poly", r))
    for tr in pcb["tracks"]:
        if tr["layer"] == layer and not tr["keepout"] and tr["net"]:
            by_net[tr["net"]].append(("track", tr))
    for a in pcb["arcs"]:
        if a["layer"] == layer and not a["keepout"] and a["net"]:
            by_net[a["net"]].append(("arc", a))
    for p in pcb["pads"]:
        if p["layer"] == layer or (p["layer"] == 74):
            key = p["net"] or f"({p['component']}.{p['name']})"
            by_net[key].append(("pad", p))
    for v in pcb["vias"]:
        if v["net"]:
            by_net[v["net"]].append(("via", v))

    for net, items in by_net.items():
        nid = names.setdefault(net, len(names) + 1)

        def draw(d, items=items):
            for kind, o in items:
                if kind == "poly":
                    d.polygon([px(x, y) for x, y in o["points"]], fill=1)
                    for h in o["holes"]:
                        if len(h) >= 3:
                            d.polygon([px(x, y) for x, y in h], fill=0)
                elif kind == "track":
                    w = max(1, int(round(o["width"] / res)))
                    a_, b_ = px(o["x1"], o["y1"]), px(o["x2"], o["y2"])
                    d.line([a_, b_], fill=1, width=w)
                    for x, y in (a_, b_):
                        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=1)
                elif kind == "arc":
                    sweep = (o["end"] - o["start"]) % 360 or 360
                    n = max(4, int(sweep / 5))
                    pts = [px(o["cx"] + o["r"] * math.cos(math.radians(o["start"] + sweep * k / n)),
                              o["cy"] + o["r"] * math.sin(math.radians(o["start"] + sweep * k / n)))
                           for k in range(n + 1)]
                    d.line(pts, fill=1, width=max(1, int(round(o["width"] / res))))
                elif kind == "pad":
                    if o["shape"] == "round" and abs(o["sx"] - o["sy"]) < 1e-3:
                        cx, cy = px(o["x"], o["y"])
                        rr = o["sx"] / 2 / res
                        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=1)
                    else:
                        d.polygon([px(x, y) for x, y in pad_corners(o)], fill=1)
                elif kind == "via":
                    cx, cy = px(o["x"], o["y"])
                    rr = o["diameter"] / 2 / res
                    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=1)
        paint(nid, draw)
    return img


def cmd_clearance(B: Boards, args) -> None:
    """
    Closest approach between copper of different nets, per layer.

    Each layer is drawn with a net id per pixel; a distance transform then
    gives every empty pixel its nearest copper, and wherever two neighbouring
    pixels lean towards different nets the gap between those nets is the sum of
    their two distances. Unconnected pads (mounting holes) count as their own
    net, written (H1.1). Accuracy is about one grid step (0.05 mm).
    """
    import numpy as np
    from scipy import ndimage

    limit = 3.0
    nrows = 80
    want = []
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--max":
            limit = float(args[i + 1])
            i += 2
        elif args[i] == "--rows":
            nrows = int(args[i + 1])
            i += 2
        elif args[i] == "--nets":
            want = [s.strip() for s in args[i + 1].split(",") if s.strip()]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    res = 0.05
    for b, pcb in pick_boards(B, rest):
        x0, y0, x1, y1 = pcb["extent"]
        x0, y0, x1, y1 = x0 - 1, y0 - 1, x1 + 1, y1 + 1
        W, H = int((x1 - x0) / res) + 1, int((y1 - y0) / res) + 1
        names: dict[str, int] = {}
        found = []
        shorts = []
        for layer in copper_layers(pcb):
            img = layer_net_image(pcb, layer, names, (x0, y1, W, H), res)
            if (img == -1).any():
                ys, xs = np.nonzero(img == -1)
                shorts.append((layer, x0 + xs[0] * res, y1 - ys[0] * res))
            cu = img != 0
            dist, (iy, ix) = ndimage.distance_transform_edt(~cu, return_indices=True)
            near = img[iy, ix]
            dist = dist * res
            best = {}
            for di, dj in ((0, 1), (1, 0), (1, 1), (1, -1)):
                if dj >= 0:
                    a_n, b_n = near[: H - di, : W - dj], near[di:, dj:]
                    a_d, b_d = dist[: H - di, : W - dj], dist[di:, dj:]
                    off = (0, 0)
                else:
                    a_n, b_n = near[: H - di, -dj:], near[di:, : W + dj]
                    a_d, b_d = dist[: H - di, -dj:], dist[di:, : W + dj]
                    off = (0, -dj)
                gap = a_d + b_d + res * (2 ** 0.5 if di and dj else 1.0)
                m = (a_n != b_n) & (a_n > 0) & (b_n > 0) & (gap <= limit)
                if not m.any():
                    continue
                ys_, xs_ = np.nonzero(m)
                g = gap[m]
                pa, pb = a_n[m], b_n[m]
                lo = np.minimum(pa, pb)
                hi = np.maximum(pa, pb)
                order = np.argsort(g)
                for k in order:
                    key = (int(lo[k]), int(hi[k]))
                    if key not in best:
                        best[key] = (float(g[k]), x0 + (xs_[k] + off[1]) * res, y1 - (ys_[k] + off[0]) * res)
            for (na, nb), (g, x, y) in best.items():
                found.append((g, layer, na, nb, x, y))
        idn = {v: k for k, v in names.items()}
        print(f"{b['title']}: copper-to-copper gaps below {limit} mm between different nets")
        for layer, x, y in shorts:
            print(f"   !! two nets overlap on {short_lname(pcb, layer)} near ({x:.1f}, {y:.1f})")
        rows = []
        for g, layer, na, nb, x, y in sorted(found):
            a_, b_ = idn[na], idn[nb]
            if want and not any(w in (a_, b_) or any(w in s for s in (a_, b_)) for w in want):
                continue
            rows.append((g, layer, a_, b_, x, y))
        for g, layer, a_, b_, x, y in rows[:nrows]:
            print(f"   {g:5.2f} mm  {short_lname(pcb, layer):<4} {a_:<24} <-> {b_:<24} near ({x:.1f}, {y:.1f})")
        if len(rows) > nrows:
            print(f"   ... {len(rows) - nrows} more (--rows N)")
        print()


# --------------------------------------------------------------------------
# board-to-board connectors checked by position, not by pin number
# --------------------------------------------------------------------------

def _signal_key(net) -> str:
    """Strip what differs between the two ends of one signal: channel suffix,
    a channel prefix, _IN/_OUT, so 'U_LS_PWM_OUT' and 'LS_PWM_U_PHASE' compare equal."""
    import re
    n = str(net or "").upper()
    n = re.sub(r"_(U|V|W)_PHASE$", "", n)
    n = re.sub(r"^(U|V|W)_", "", n)
    n = re.sub(r"_(IN|OUT)$", "", n)
    return n


def mating_points(pcb: dict, des: str) -> list[tuple[float, float, str, str]]:
    """Where the pins of a connector physically are, and the net on each.

    A through-hole pad is a pin. An SMD header puts its pins in plated or
    locating holes that often carry no net and a dummy pad number; the net then
    comes from the nearest netted pad of the same part (its solder leg). Pin
    numbers are deliberately not used: two headers that mate can number the
    same physical pin differently (row-wise vs column-wise, SMD vs THT)."""
    pads = [p for p in pcb["pads"] if p["component"] == des]
    holes = [p for p in pads if p["hole"] > 0]
    netted = [p for p in pads if p["net"]]
    pts = []
    for h in (holes or pads):
        if h["net"]:
            pts.append((h["x"], h["y"], h["name"], h["net"]))
        elif netted:
            q = min(netted, key=lambda p: (p["x"] - h["x"]) ** 2 + (p["y"] - h["y"]) ** 2)
            pts.append((h["x"], h["y"], f"{q['name']}(via leg)", q["net"]))
    return pts


def _pcb_designator(pcb: dict, connector: str) -> str | None:
    names = {c["designator"] for c in pcb["components"]}
    if connector in names:
        return connector
    hits = [n for n in names if n.startswith(connector + "_") or n.startswith(connector)]
    return sorted(hits, key=len)[0] if hits else None


def mate_report(B: "Boards", lk: dict) -> dict | None:
    """Pair the two connectors of one interboard link pin by pin, by position."""
    a = B.pick(lk["a_board"]); b = B.pick(lk["b_board"])
    if not a or not b:
        return None
    da, db = _pcb_designator(a[1], lk["a_connector"]), _pcb_designator(b[1], lk["b_connector"])
    if not da or not db:
        return None
    pa, pb = mating_points(a[1], da), mating_points(b[1], db)
    if not pa or not pb:
        return None
    # boards are stacked in one frame up to a small offset: take the median shift
    import statistics
    shifts = []
    for x, y, *_ in pa:
        q = min(pb, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
        shifts.append((q[0] - x, q[1] - y))
    dx = statistics.median(s[0] for s in shifts); dy = statistics.median(s[1] for s in shifts)
    rows, worst = [], 0.0
    for x, y, an, anet in pa:
        q = min(pb, key=lambda q: (q[0] - x - dx) ** 2 + (q[1] - y - dy) ** 2)
        r = math.hypot(q[0] - x - dx, q[1] - y - dy)
        worst = max(worst, r)
        rows.append({"x": x, "y": y, "a_pad": an, "a_net": anet, "b_pad": q[2], "b_net": q[3],
                     "same": _signal_key(anet) == _signal_key(q[3]), "resid": r})
    return {"a": (lk["a_board"], da), "b": (lk["b_board"], db), "shift": (dx, dy),
            "worst_resid": worst, "rows": rows, "all_same": all(r["same"] for r in rows)}


def cmd_mate(B: Boards, args) -> None:
    want = (args[0].upper() if args else "")
    for lk in B.model.get("interboard_links", []):
        if want and want not in lk["designator"].upper() and \
           want not in (lk["a_connector"] + lk["b_connector"]).upper():
            continue
        r = mate_report(B, lk)
        if not r:
            print(f"{lk['designator']}: connector not found on both layouts")
            continue
        verdict = "every pin meets the same signal" if r["all_same"] else \
            "!! SIGNAL MISMATCH -- check the pins marked ?"
        print(f"{lk['designator']}  {r['a'][0]} {r['a'][1]} <-> {r['b'][0]} {r['b'][1]}   "
              f"offset {r['shift'][0]:+.2f},{r['shift'][1]:+.2f} mm, worst pin miss "
              f"{r['worst_resid']:.2f} mm   {verdict}")
        if r["worst_resid"] > 0.5:
            print("   !! pins do not line up within 0.5 mm -- connector positions differ between boards")
        if not r["all_same"] or len(args) > 0:
            for w in r["rows"]:
                print(f"   {'ok' if w['same'] else '? '} ({w['x']:.2f},{w['y']:.2f})  "
                      f"{w['a_pad']:<12} {str(w['a_net']):<24} <-> {w['b_pad']:<14} {w['b_net']}")


def pcb_cross_check(model: dict) -> dict:
    """For each board: does the PcbDoc's pad connectivity (Altium's own compiled
    netlist, pushed in by the last ECO) agree with the offline netlist?
    Returns {board title: (clean, summary)}; boards without a layout are absent."""
    out = {}
    specs = {b["key"]: b for b in P.spec["boards"]}
    for b in model["boards"]:
        spec = specs.get(b["key"])
        paths = pcb_paths(spec) if spec else []
        if not paths:
            continue
        pcb = load_pcb(paths[0])
        r = sync_report(b, pcb)
        n = {k: len(r[k]) for k in ("missing_in_pcb", "extra_in_pcb", "split", "merged", "unnetted")}
        bad = sum(n.values())
        when = datetime.fromtimestamp(Path(paths[0]).stat().st_mtime).isoformat(timespec="minutes")
        out[b["title"]] = (bad == 0, f"{Path(paths[0]).name} saved {when}"
                           + ("" if bad == 0 else ", " + ", ".join(f"{v} {k}" for k, v in n.items() if v)))
    return out


def cmd_length(B: Boards, args) -> None:
    """Pad-to-pad routed length on one net, along the copper (pcb_length)."""
    import pcb_length
    if len(args) < 2:
        sys.exit("pcb length needs two pads on one net: pcb length IC1.112 R50.2")
    for b, pcb, ra in B.resolve(args[0]):
        rb = args[1].split("/", 1)[-1]
        pa, pb = find_pad(pcb, ra), find_pad(pcb, rb)
        if not (pa and pb):
            continue
        if pa["net"] != pb["net"]:
            print(f"{b['title']}: {ra} is on {pa['net']}, {rb} on {pb['net']} -- not one net "
                  f"(through a series part use: pcb xsignals SRC DST)")
            continue
        r = pcb_length.connection(pcb, pa, pb, pad_corners, copper_layers(pcb))
        raw = sum(track_len(t) for t in pcb["tracks"] if t["net"] == pa["net"] and altium_pcb.is_copper(t["layer"]))
        raw += sum(arc_len(a) for a in pcb["arcs"] if a["net"] == pa["net"] and altium_pcb.is_copper(a["layer"]))
        print(f"{b['title']}: {ra} -> {rb}  ({pa['net']})")
        if not r:
            print("   NOT joined by tracks/vias (a pour may join them; this measure follows tracks only)")
            continue
        lay = ", ".join(short_lname(pcb, l) for l in r["layers"])
        print(f"   routed {r['planar']:.2f} mm planar, {r['total']:.2f} mm with via barrels "
              f"(+{r['z']:.2f}), {r['vias']} via(s), layers {lay}")
        print(f"   (sum of every track on the net: {raw:.2f} mm)")


def cmd_xsignals(B: Boards, args) -> None:
    """
    Every signal from part SRC to part DST, direct or through one series part,
    with routed length, and the length-matching check if asked:
      pcb xsignals IC1 U1 --ref FMC_SDCLK --tol 10 --group data=FMC_D*,FMC_NBL*
    --after-series measures only the leg from the series part to DST, the way
    length matching is often set up when the resistor sits at the driver.
    """
    import pcb_length
    if len(args) < 2:
        sys.exit("pcb xsignals needs two parts: pcb xsignals IC1 U1 [--ref NET] [--tol MM] "
                 "[--group NAME=PAT,PAT ...] [--z] [--after-series]")
    src, dst, rest = args[0], args[1], args[2:]
    ref, tol, use_z, groups, after = None, None, False, [], False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--ref" and i + 1 < len(rest):
            ref = rest[i + 1]; i += 2
        elif a == "--tol" and i + 1 < len(rest):
            tol = float(rest[i + 1]); i += 2
        elif a == "--group" and i + 1 < len(rest):
            nm, _, pats = rest[i + 1].partition("=")
            groups.append((nm, pats.split(","))); i += 2
        elif a == "--z":
            use_z = True; i += 1
        elif a == "--after-series":
            after = True; i += 1
        else:
            i += 1
    key = ("after_total" if use_z else "after_planar") if after else ("total" if use_z else "planar")
    for b, pcb, s in B.resolve(src):
        d = dst.split("/", 1)[-1]
        rows = pcb_length.xsignals(pcb, s, d, pad_corners, copper_layers(pcb))
        if not rows:
            print(f"{b['title']}: no connection from {s} to {d}")
            continue
        refrow = next((r for r in rows if ref and ref in r["nets"]), None)
        print(f"{b['title']}: {s} -> {d}, {len(rows)} signal(s); length = "
              f"{'planar + via barrels' if use_z else 'planar (tracks + arcs)'}, mm"
              + (", series part -> DST leg only" if after else ", whole path"))
        if ref and not refrow:
            print(f"   !! reference {ref} not found among these signals")
        print(f"   {'signal':<14}{'src':>8} {'dst':>8} {'series':>7} {'length':>8} {'vias':>5}"
              + (f" {'vs ' + ref:>12}" if refrow else ""))
        for r in sorted(rows, key=lambda r: r["name"]):
            if not r["ok"]:
                print(f"   {r['name']:<14}{r['src']['name']:>8} {r['dst']['name']:>8} "
                      f"{r['via_part'] or '-':>7}  NOT fully routed")
                continue
            dv = f" {r[key] - refrow[key]:>+12.1f}" if refrow else ""
            print(f"   {r['name']:<14}{r['src']['name']:>8} {r['dst']['name']:>8} "
                  f"{r['via_part'] or '-':>7} {r[key]:>8.1f} {r['vias']:>5}{dv}")
        for nm, pats in groups:
            g = [r for r in rows if r["ok"] and pcb_length.glob_match(r["name"], pats)]
            if not g:
                continue
            lo, hi = min(g, key=lambda r: r[key]), max(g, key=lambda r: r[key])
            line = (f"   group {nm}: {len(g)} signal(s), {lo[key]:.1f}..{hi[key]:.1f}, "
                    f"spread {hi[key] - lo[key]:.1f}")
            if tol is not None:
                line += "  OK" if hi[key] - lo[key] <= tol else f"  !! over {tol:g}"
            print(line)
        if refrow and tol is not None:
            bad = [r for r in rows if r["ok"] and r is not refrow and abs(r[key] - refrow[key]) > tol]
            print(f"   vs {ref} within +-{tol:g}: "
                  + ("all OK" if not bad else
                     "!! " + ", ".join(f"{r['name']} {r[key] - refrow[key]:+.1f}" for r in bad)))


def read_rules(path) -> dict:
    """Design rules, net classes and xSignals straight from the PcbDoc."""
    import re
    doc = altium_pcb.Doc(path)
    raw = doc.ole.openstream("Rules6/Data").read().decode("latin-1")
    rules = []
    for rec in re.split(r"[\x00-\x08]{2,}", raw):
        f = dict(x.split("=", 1) for x in rec.split("|") if "=" in x)
        if f.get("RULEKIND"):
            rules.append({k: re.split(r"[\x00-\x1f]", v)[0] for k, v in f.items()})
    kinds = {"0": "net", "1": "component", "10": "xsignal"}
    classes = []
    for c in doc.text_records("Classes6"):
        members = [v for k, v in sorted(c.items()) if k.startswith("M") and k[1:].isdigit()]
        classes.append({"name": c.get("NAME", ""), "kind": kinds.get(c.get("KIND", ""), c.get("KIND", "")),
                        "members": members})
    sig = doc.ole.openstream("SignalClasses/Data").read().decode("latin-1")
    for rec in re.split(r"[\x00-\x08]{2,}", sig):
        f = dict(x.split("=", 1) for x in rec.split("|") if "=" in x)
        if f.get("NAME"):
            members = [v for k, v in sorted(f.items()) if k.startswith("M") and k[1:].isdigit()]
            classes.append({"name": re.split(r"[\x00-\x1f]", f["NAME"])[0], "kind": "xsignal", "members": members})
    pin_pairs = doc.ole.get_size("PinPairsSection/Data") if doc.ole.exists("PinPairsSection/Data") else 0
    return {"rules": rules, "classes": classes, "pin_pair_bytes": pin_pairs}


def cmd_rules(B: Boards, args) -> None:
    """
    pcb rules [Board] [kind]   design rules (scope, key values), net classes, xSignals.
    kind filters by rule kind, e.g. `pcb rules length` for Length/MatchedLengths.
    """
    want = args[-1].lower() if args and not B.pick(args[-1]) else None
    keys = ("TOLERANCE", "MINLIMIT", "MAXLIMIT", "GAP", "MINWIDTH", "MAXWIDTH", "PREFEREDWIDTH",
            "CHECKXSIGNALS", "CHECKOTHERS", "CHECKNETSINDIFFPAIR")
    for b, pcb in B.items:
        path = Path(pcb["file"])
        info = read_rules(path)
        print(f"{b['title']}  ({path.name})")
        for r in info["rules"]:
            if want and want not in r["RULEKIND"].lower():
                continue
            extra = " ".join(f"{k.lower()}={r[k]}" for k in keys if k in r)
            print(f"   {r['RULEKIND']:<22} {r.get('NAME', ''):<24} {'on ' if r.get('ENABLED') == 'TRUE' else 'off'} "
                  f"scope {r.get('SCOPE1EXPRESSION', '')}"
                  + (f" / {r['SCOPE2EXPRESSION']}" if r.get("SCOPE2EXPRESSION", "All") != "All" else "")
                  + (f"   {extra}" if extra else ""))
        if not want:
            for c in info["classes"]:
                if c["members"] or c["kind"] == "xsignal":
                    print(f"   class [{c['kind']}] {c['name']}: {len(c['members'])} member(s)"
                          + (f" -- {', '.join(c['members'][:12])}{' ...' if len(c['members']) > 12 else ''}"
                             if c["members"] else ""))
            xs = [c for c in info["classes"] if c["kind"] == "xsignal" and c["members"]]
            if not xs and not info["pin_pair_bytes"]:
                print("   no xSignals defined: a net split by a series part is two separate nets to every rule")


COMMANDS = {"summary": cmd_summary, "check": cmd_check, "stack": cmd_stack,
            "part": cmd_part, "net": cmd_net, "near": cmd_near, "dist": cmd_dist,
            "render": cmd_render, "path": cmd_path, "caps": cmd_caps,
            "routed": cmd_routed, "clearance": cmd_clearance, "mate": cmd_mate,
            "length": cmd_length, "xsignals": cmd_xsignals, "rules": cmd_rules}


def main(argv: list[str]) -> int:
    argv, ref = project.take_project_arg(argv)
    project.use(ref)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if not argv else 2
    B = Boards()
    query.print_caveats()
    if not B.items:
        print("no .PcbDoc found for any board of this project")
        return 1
    COMMANDS[argv[0]](B, argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
