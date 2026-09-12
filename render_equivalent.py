#!/usr/bin/env python3
"""
Draw the whole controller as ONE equivalent circuit, with the board boundaries
dissolved.

The per-sheet drawings in `schematics/` show the design as it was drawn. This
shows it as it works: nets are merged across every board-to-board connector, so
a signal runs from an MCU pin through the buffer, the connector, the gate
driver and out to the MOSFET as a single wire on a single page.

Three reductions make that page readable, and each is the standard one for an
equivalent circuit:

  * Parallel decoupling collapses. A capacitor sitting across two supply rails
    carries no information about how the circuit works, so a rail's decoupling
    is replaced by one aggregated symbol per rail per block.
  * Supply and ground nets are drawn as power symbols at each pin instead of as
    wires. GND alone has 206 pins; drawn as wire it is a black smear.
  * Repeated channels keep their own row. The three phases are drawn in full,
    in three horizontal bands, so the symmetry is visible and a broken one
    stands out.

Run:  python render_equivalent.py [--all-passives] [--fanout N]
Out:  equivalent_circuit.html
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from altium_sch import PIN_DIR, Sheet, parse_sheet, primitive_points
import model_io
import project
from boards import BOARDS, sheet_paths
from project import P

ROOT = Path(__file__).resolve().parent

from freshness import atomic_write_text  # noqa: E402

# --- tuning ---------------------------------------------------------------
RAIL_FANOUT = 8          # nets bigger than this are drawn as power symbols
COL_GAP = 150            # routing channel between columns
BAND_GAP = 54
COMP_GAP_X, COMP_GAP_Y = 46, 34
SUBCOL_GAP = 40
TARGET_STACK = 900       # a cell taller than this wraps into more sub-columns
MARGIN = 46
LANE_STEP = 11           # spacing between trunks in a routing channel

PHASE_TOKENS = ("U", "V", "W")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


_short = None


def set_sheet_names(names) -> None:
    """Derive the sheet-name shortener from this project's own sheet list."""
    global _short
    from naming import shortener
    _short = shortener(list(names))


def short_sheet(name: str) -> str:
    if _short is None:
        return (name or "").replace(".SchDoc", "")
    return _short(name)


def domain(name: str) -> str:
    n = (name or "").upper()
    if n in ("GND", "AGND", "DGND", "PGND") or n.startswith("GND") or n.endswith("_GND"):
        return "gnd"
    if n[:1] in "+-" or n.startswith(("VDD", "VCC", "VSS", "AVDD", "A5V", "VREF",
                                      "AVREF", "AVBIAS", "VDDA", "VDDIO", "VRAW",
                                      "DC+", "DC-")):
        return "pwr"
    if n.startswith("NET"):
        return "local"
    return "sig"


# --------------------------------------------------------------------------
# the merged electrical model
# --------------------------------------------------------------------------

class DSU:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def merge_nets(model: dict):
    """
    One net set for the whole system.

    Every board-to-board wire in the multi-board schematic joins the net on
    each side, so the connectors stop being endpoints and the signal becomes
    continuous -- which is the entire point of drawing an equivalent circuit
    rather than three separate schematics.
    """
    dsu = DSU()
    title_key = {b["title"]: b["key"] for b in model["boards"]}
    for b in model["boards"]:
        for n in b["nets"]:
            dsu.find((b["key"], n))
    for lk in model["interboard_links"]:
        ak, bk = title_key[lk["a_board"]], title_key[lk["b_board"]]
        for w in lk["wires"]:
            if w["a_net"] and w["b_net"]:
                dsu.union((ak, w["a_net"]), (bk, w["b_net"]))

    nets: dict = defaultdict(lambda: {"names": set(), "nodes": [], "domain": "sig"})
    for b in model["boards"]:
        for name, rec in b["nets"].items():
            root = dsu.find((b["key"], name))
            g = nets[root]
            g["names"].add(name)
            for nd in rec["nodes"]:
                g["nodes"].append({"board": b["key"], "designator": nd["designator"],
                                   "pin": nd["pin"], "pin_name": nd.get("pin_name"),
                                   "sheet": nd["sheet"]})
    out = {}
    for root, g in nets.items():
        # prefer a name that is not auto-generated, then the shortest
        names = sorted(g["names"], key=lambda s: (s.startswith("Net"), len(s), s))
        name = names[0]
        out[root] = {"name": name, "aliases": names[1:], "nodes": g["nodes"],
                     "domain": domain(name)}
    return out


def phase_of(designator: str, room: str | None, net_names: list[str]) -> str | None:
    """
    Which phase a part belongs to.

    Channel parts say so in their room. Everything else is inferred from the
    nets it touches: the control board's buffers and current-sense channels are
    per-phase without being in a room, and putting them in the wrong band would
    make the drawing lie about the symmetry.
    """
    if room:
        for t in PHASE_TOKENS:
            if room.upper().startswith(t + "_") or room.upper() == t:
                return t
    counts = Counter()
    for n in net_names:
        for tok in re.split(r"[_+\-]", (n or "").upper()):
            if tok in PHASE_TOKENS:
                counts[tok] += 1
    if not counts:
        return None
    top, n = counts.most_common(1)[0]
    total = sum(counts.values())
    # a clear majority is enough; a two-pin series part often touches only one
    # phase net, and demanding two mentions would leave every gate resistor in
    # the shared band instead of beside the channel it belongs to
    return top if n >= total * 0.6 else None


def column_order(model: dict) -> list[tuple[str, str]]:
    """
    Left-to-right order of (board, sheet).

    Within a board, sheets are ordered by how far they sit from the sheets that
    carry the board-to-board connectors, so the drawing flows from the parts
    facing the outside world towards the parts facing the next board.
    """
    order: list[tuple[str, str]] = []
    for b in model["boards"]:
        facing = {lk[f"{s}_block"] for lk in model["interboard_links"]
                  for s in ("a", "b")
                  if lk[f"{s}_board"] == b["title"] and lk[f"{s}_block"]}
        adj: dict[str, set] = defaultdict(set)
        for bl in b["block_links"]:
            adj[bl["a"]].add(bl["b"])
            adj[bl["b"]].add(bl["a"])
        dist = {s: 0 for s in facing}
        frontier = set(facing)
        d = 0
        while frontier:
            d += 1
            nxt = set()
            for s in frontier:
                for t in adj[s]:
                    if t not in dist:
                        dist[t] = d
                        nxt.add(t)
            frontier = nxt
        sheets = [blk["sheet"] for blk in b["blocks"]]
        far = max(dist.values(), default=0) + 1
        sheets.sort(key=lambda s: (-dist.get(s, far), s))
        order += [(b["key"], s) for s in sheets]
    return order


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------

class Symbol:
    """One component's graphics and pin anchors in component-local coordinates
    (origin at the component's placement point, Y already flipped for SVG)."""

    def __init__(self, comp, graphics):
        self.designator = comp.designator
        self.comment = comp.comment
        self.lib_ref = comp.lib_ref
        self.prims: list[tuple[str, list]] = []
        self.pins: list[dict] = []
        xs, ys = [], []

        def rel(px, py):
            return px - comp.x, comp.y - py

        for g in graphics:
            if g["part_id"] not in (-1, comp.part_id):
                continue
            kv = g["raw"]
            pts = [rel(px, py) for px, py in primitive_points(kv)]
            if not pts:
                continue
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
            self.prims.append((g["kind"], pts))
        for pin in comp.pins:
            a = rel(*pin.end)
            b = rel(pin.x, pin.y)
            dx, dy = PIN_DIR.get(pin.conglomerate & 0x03, (1, 0))
            self.pins.append({"designator": pin.designator, "name": pin.name,
                              "anchor": a, "root": b, "dir": (dx, -dy)})
            xs += [a[0], b[0]]
            ys += [a[1], b[1]]
        if not xs:
            xs, ys = [0, 20], [0, 20]
        self.x0, self.y0, self.x1, self.y1 = min(xs), min(ys), max(xs), max(ys)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def draw(self, tx: float, ty: float) -> str:
        """SVG for the symbol body, translated so its top-left lands at tx,ty."""
        ox, oy = tx - self.x0, ty - self.y0
        out = []
        for kind, pts in self.prims:
            p = [(x + ox, y + oy) for x, y in pts]
            if kind == "rectangle" and len(p) >= 2:
                (ax, ay), (bx, by) = p[0], p[1]
                out.append(f'<rect class="sym" x="{min(ax, bx):.1f}" y="{min(ay, by):.1f}" '
                           f'width="{abs(bx - ax):.1f}" height="{abs(by - ay):.1f}"/>')
            elif kind in ("ellipse", "arc", "elliptical_arc") and len(p) >= 2:
                cx, cy = p[0]
                rx = abs(p[-1][0] - cx) or 3
                ry = abs(p[-1][1] - cy) or rx
                out.append(f'<ellipse class="sym" cx="{cx:.1f}" cy="{cy:.1f}" '
                           f'rx="{rx:.1f}" ry="{ry:.1f}"/>')
            elif len(p) >= 2:
                d = " ".join(f"{'M' if i == 0 else 'L'} {x:.1f} {y:.1f}"
                             for i, (x, y) in enumerate(p))
                if kind == "polygon":
                    d += " Z"
                out.append(f'<path class="sym{" fillsym" if kind == "polygon" else ""}" d="{d}"/>')
        return "".join(out), (ox, oy)


def load_symbols() -> dict[tuple[str, str], Symbol]:
    """Symbol per (board, logical designator), read from the source sheets."""
    out: dict[tuple[str, str], Symbol] = {}
    for key in BOARDS:
        for p in sheet_paths(key):
            sheet = parse_sheet(p)
            by_owner = defaultdict(list)
            for g in sheet.graphics:
                by_owner[g["owner"] + 1].append(g)
            for c in sheet.components:
                if c.designator:
                    out[(key, c.designator)] = Symbol(c, by_owner.get(c.index, []))
    return out


# --------------------------------------------------------------------------
# selection, placement, routing
# --------------------------------------------------------------------------

SIGNIFICANT = {"ic", "switch", "power_module", "connector"}


def link_index(model: dict) -> dict[tuple[str, str], list[dict]]:
    """(board, connector designator) -> the board-to-board links it carries."""
    title_key = {b["title"]: b["key"] for b in model["boards"]}
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for lk in model["interboard_links"]:
        for side, peer in (("a", "b"), ("b", "a")):
            key = (title_key[lk[f"{side}_board"]], lk[f"{side}_connector"])
            out[key].append({"link": lk["designator"],
                             "peer_board": lk[f"{peer}_board"],
                             "peer_connector": lk[f"{peer}_connector"],
                             "wires": lk["wire_count"]})
    return out


def select(model: dict, nets: dict, keep_all_passives: bool):
    """
    Which parts belong on an equivalent circuit.

    Everything active, every connector and every part with three or more pins
    stays. A two-pin passive stays only if it is in series in a path -- a gate
    resistor, a shunt, a filter element. One sitting across two supply rails is
    decoupling: it is counted per block and reported, not drawn.
    """
    node_nets: dict[tuple[str, str], list] = defaultdict(list)
    for root, net in nets.items():
        for nd in net["nodes"]:
            node_nets[(nd["board"], nd["designator"])].append(root)

    rails = {root for root, n in nets.items()
             if n["domain"] in ("pwr", "gnd") or len(n["nodes"]) > RAIL_FANOUT}

    kept, dropped = {}, Counter()
    for b in model["boards"]:
        for des, c in b["components"].items():
            key = (b["key"], des)
            roots = node_nets.get(key, [])
            if c["kind"] in SIGNIFICANT or c["pin_count"] >= 3:
                kept[key] = c
                continue
            if c["kind"] in ("testpoint", "mechanical"):
                dropped[(b["key"], c["sheet"], c["kind"])] += 1
                continue
            # in series in a path: neither end is a supply rail. A pull-up or a
            # bypass has one end on a rail and contributes no topology, so it is
            # counted instead of drawn; a gate resistor, a shunt, a CAN
            # termination or a series damping resistor has both ends on signals
            # and stays.
            series = len(roots) >= 2 and not any(r in rails for r in roots)
            if keep_all_passives or series:
                kept[key] = c
            else:
                dropped[(b["key"], c["sheet"], "decoupling")] += 1
    return kept, dropped, rails, node_nets


def layout(model, kept, symbols, nets, node_nets):
    cols = column_order(model)
    col_index = {c: i for i, c in enumerate(cols)}

    cells: dict[tuple[int, str], list] = defaultdict(list)
    placed: dict[tuple[str, str], dict] = {}
    for key, c in kept.items():
        board, des = key
        ci = col_index.get((board, c["sheet"]))
        if ci is None:
            continue
        names = [nets[r]["name"] for r in node_nets.get(key, [])]
        band = phase_of(des, c.get("room"), names) or "-"
        cells[(ci, band)].append(key)

    bands = ["-"] + [t for t in PHASE_TOKENS if any(b == t for _, b in cells)]

    def sym_of(key):
        board, des = key
        logical = kept[key].get("logical_designator") or des
        return symbols.get((board, logical)) or symbols.get((board, des))

    def pack(keys):
        """Greedily wrap a cell's parts into sub-columns of bounded height.

        A single tall stack is what turned this drawing into a 9000-unit strip;
        wrapping keeps every band a readable height at the cost of a wider page,
        which scrolls far better."""
        subcols, cur, h = [], [], 0.0
        for key in keys:
            sym = sym_of(key)
            if sym is None:
                continue
            if cur and h + sym.h + COMP_GAP_Y > TARGET_STACK:
                subcols.append(cur)
                cur, h = [], 0.0
            cur.append((key, sym))
            h += sym.h + COMP_GAP_Y
        if cur:
            subcols.append(cur)
        return subcols

    packed = {(ci, band): pack(sorted(cells.get((ci, band), []), key=lambda k: k[1]))
              for ci in range(len(cols)) for band in bands}

    # column width: widest arrangement of sub-columns across the bands
    col_w = {}
    for ci in range(len(cols)):
        w = 0.0
        for band in bands:
            sc = packed[(ci, band)]
            if not sc:
                continue
            width = sum(max(s.w for _, s in col) for col in sc)                 + SUBCOL_GAP * (len(sc) - 1)
            w = max(w, width)
        col_w[ci] = w + COMP_GAP_X

    band_h = {}
    for band in bands:
        h = 0.0
        for ci in range(len(cols)):
            for col in packed[(ci, band)]:
                h = max(h, sum(s.h for _, s in col) + COMP_GAP_Y * (len(col) - 1))
        band_h[band] = h

    # Two column grids, not one. The shared subsystems occupy the left of the
    # sheet order and the per-phase chain the right, so a single grid leaves the
    # three phase bands empty for most of the page. Each group gets its own
    # packing; the three phase bands share theirs, so they still line up with
    # each other and the symmetry stays readable.
    def grid(band_set):
        used = [ci for ci in range(len(cols))
                if any(packed[(ci, b)] for b in band_set)]
        xs, x = {}, MARGIN
        for ci in used:
            xs[ci] = x
            x += col_w[ci] + COL_GAP
        return xs, (x - COL_GAP + MARGIN if used else MARGIN)

    phase_bands = [b for b in bands if b != "-"]
    grid_shared, w_shared = grid(["-"])
    grid_phase, w_phase = grid(phase_bands)
    col_x = {"-": grid_shared, "phase": grid_phase}
    total_w = max(w_shared, w_phase)

    band_y, y = {}, MARGIN + 54
    for band in bands:
        band_y[band] = y
        y += band_h[band] + BAND_GAP
    total_h = y + MARGIN

    for ci in range(len(cols)):
        for band in bands:
            gx = col_x["-" if band == "-" else "phase"]
            if ci not in gx:
                continue
            sx = gx[ci]
            for col in packed[(ci, band)]:
                stack = sum(s.h for _, s in col) + COMP_GAP_Y * (len(col) - 1)
                cy = band_y[band] + (band_h[band] - stack) / 2
                width = max(s.w for _, s in col)
                for key, sym in col:
                    placed[key] = {"sym": sym, "x": sx, "y": cy, "col": ci,
                                   "band": band, "comp": kept[key]}
                    cy += sym.h + COMP_GAP_Y
                sx += width + SUBCOL_GAP

    return {"cols": cols, "col_x": col_x, "col_w": col_w, "bands": bands,
            "band_y": band_y, "band_h": band_h, "placed": placed,
            "size": (total_w, total_h)}


def anchors(placed_rec):
    """Absolute pin anchor points for a placed component."""
    sym = placed_rec["sym"]
    ox = placed_rec["x"] - sym.x0
    oy = placed_rec["y"] - sym.y0
    out = {}
    for p in sym.pins:
        out[p["designator"]] = {
            "x": p["anchor"][0] + ox, "y": p["anchor"][1] + oy,
            "dir": p["dir"], "name": p["name"],
        }
    return out


def route(lay, nets, rails, fanout_limit: int):
    """
    Wires for signal nets, power symbols for rails.

    Each signal net is drawn as a chain: its pins are sorted left to right and
    consecutive pins are joined by a three-segment orthogonal path that drops
    into the routing channel between their columns. A net is one electrical
    node, so any spanning path is faithful, and a chain keeps the wire short
    where a single trunk would send two long horizontals across the page.
    """
    placed = lay["placed"]
    anchor_cache = {k: anchors(v) for k, v in placed.items()}
    col_x, col_w = lay["col_x"], lay["col_w"]
    lanes: dict[int, int] = defaultdict(int)

    def channel(rec_band: str, ci: int) -> float:
        gx = col_x["-" if rec_band == "-" else "phase"]
        return gx.get(ci, MARGIN) + col_w[ci] + 14

    wires, glyphs, stubs = [], [], []
    stats = Counter()

    for root, net in nets.items():
        pins = []
        for nd in net["nodes"]:
            key = (nd["board"], nd["designator"])
            rec = placed.get(key)
            if not rec:
                continue
            a = anchor_cache[key].get(nd["pin"])
            if a:
                pins.append({"x": a["x"], "y": a["y"], "dir": a["dir"],
                             "col": rec["col"], "band": rec["band"],
                             "board": nd["board"], "des": nd["designator"],
                             "pin": nd["pin"], "pin_name": a["name"]})
        if not pins:
            continue
        name = net["name"]
        is_rail = root in rails
        if is_rail or len(pins) > fanout_limit:
            stats["rail"] += 1
            for p in pins:
                glyphs.append({"net": name, "domain": net["domain"], **p})
            continue
        if len(pins) == 1:
            if not name.startswith("Net"):
                stubs.append({"net": name, "domain": net["domain"], **pins[0]})
                stats["stub"] += 1
            continue
        stats["wire"] += 1
        crossing = len({p["board"] for p in pins}) > 1
        if crossing:
            stats["crossing"] += 1
        pins.sort(key=lambda p: (p["x"], p["y"]))
        segs = []
        for a, b in zip(pins, pins[1:]):
            ci = max(a["col"], 0)
            lane = lanes[ci]
            lanes[ci] = (lanes[ci] + 1) % 11
            xm = channel(a["band"], ci) + lane * LANE_STEP
            if b["x"] <= xm:                     # both sides left of the channel
                xm = min(a["x"], b["x"]) - 14 - lane * LANE_STEP
            segs.append(f"M {a['x']:.1f} {a['y']:.1f} H {xm:.1f} "
                        f"V {b['y']:.1f} H {b['x']:.1f}")
        wires.append({"net": name, "domain": net["domain"], "d": " ".join(segs),
                      "pins": pins, "crossing": crossing})
    return wires, glyphs, stubs, stats


def glyph_svg(g: dict) -> str:
    """A power-port mark pointing away from the pin it serves."""
    dx, dy = g["dir"]
    x, y = g["x"], g["y"]
    ex, ey = x + dx * 9, y + dy * 9
    px, py = -dy, dx
    cls = "gnd" if g["domain"] == "gnd" else "pwr"
    parts = [f'<line class="rail {cls}" x1="{x:.1f}" y1="{y:.1f}" '
             f'x2="{ex:.1f}" y2="{ey:.1f}"/>']
    if g["domain"] == "gnd":
        for i, half in enumerate((6, 4, 2)):
            bx, by = ex + dx * i * 2.6, ey + dy * i * 2.6
            parts.append(f'<line class="rail {cls}" x1="{bx - px * half:.1f}" '
                         f'y1="{by - py * half:.1f}" x2="{bx + px * half:.1f}" '
                         f'y2="{by + py * half:.1f}"/>')
    else:
        parts.append(f'<line class="rail {cls}" x1="{ex - px * 6:.1f}" '
                     f'y1="{ey - py * 6:.1f}" x2="{ex + px * 6:.1f}" '
                     f'y2="{ey + py * 6:.1f}"/>')
        tx, ty = ex + dx * 14, ey + dy * 14
        anchor = "middle" if dx == 0 else ("start" if dx > 0 else "end")
        parts.append(f'<text class="t-rail {cls}" x="{tx:.1f}" y="{ty + 3:.1f}" '
                     f'text-anchor="{anchor}">{esc(g["net"])}</text>')
    return "".join(parts)


def board_regions(model, lay):
    """
    The x extent each board occupies, per column grid.

    Columns are already grouped by board, so a board is a contiguous run --
    tinting it is what lets a reader answer "which board is this part on"
    without reading every column header.
    """
    order = [b["key"] for b in model["boards"]]
    regions = []
    for group, bandset in (("-", ["-"]),
                           ("phase", [b for b in lay["bands"] if b != "-"])):
        gx = lay["col_x"][group]
        if not gx or not bandset:
            continue
        top = min(lay["band_y"][b] for b in bandset) - 30
        bottom = max(lay["band_y"][b] + lay["band_h"][b] for b in bandset) + 22
        per: dict[str, list] = defaultdict(list)
        for ci, (board, _sheet) in enumerate(lay["cols"]):
            if ci in gx:
                per[board].append(ci)
        for board, cis in per.items():
            x0 = min(gx[ci] for ci in cis) - 18
            x1 = max(gx[ci] + lay["col_w"][ci] for ci in cis) - COMP_GAP_X + 18
            regions.append({"board": board, "group": group, "x0": x0, "x1": x1,
                            "y0": top - 26, "y1": bottom,
                            "tint": order.index(board) % 3})
    return regions


def render(model, lay, wires, glyphs, stubs, dropped, links) -> str:
    W, H = lay["size"]
    placed = lay["placed"]
    out = [f'<svg viewBox="0 0 {W:.0f} {H:.0f}" xmlns="http://www.w3.org/2000/svg" '
           f'class="eq" role="img" aria-label="equivalent circuit">']
    add = out.append
    add(f'<rect class="page" x="0" y="0" width="{W:.0f}" height="{H:.0f}"/>')

    # one tinted region per board, so board membership is readable at a glance
    titles = {b["key"]: b["title"] for b in model["boards"]}
    for r in board_regions(model, lay):
        add(f'<rect class="boardzone t{r["tint"]}" x="{r["x0"]:.0f}" y="{r["y0"]:.0f}" '
            f'width="{r["x1"] - r["x0"]:.0f}" height="{r["y1"] - r["y0"]:.0f}" rx="12"/>')
        add(f'<text class="t-board t{r["tint"]}" x="{r["x0"] + 12:.0f}" '
            f'y="{r["y0"] + 17:.0f}">{esc(titles[r["board"]])}</text>')

    # phase bands
    for band in lay["bands"]:
        if band == "-":
            continue
        y, h = lay["band_y"][band], lay["band_h"][band]
        add(f'<rect class="band" x="{MARGIN / 2:.0f}" y="{y - 16:.0f}" '
            f'width="{W - MARGIN:.0f}" height="{h + 32:.0f}" rx="10"/>')
        add(f'<text class="t-band" x="{MARGIN / 2 + 10:.0f}" y="{y - 22:.0f}">'
            f'{esc(band)} PHASE</text>')

    # column headers
    boards = {b["key"]: b["title"] for b in model["boards"]}
    for group, ypos in (("-", MARGIN + 14), ("phase", None)):
        gx = lay["col_x"][group]
        for ci, (board, sheet) in enumerate(lay["cols"]):
            if ci not in gx:
                continue
            if group == "-":
                y = MARGIN + 14
            else:
                first = min(lay["band_y"][b] for b in lay["bands"] if b != "-")
                y = first - 30
            add(f'<text class="t-col" x="{gx[ci]:.0f}" y="{y:.0f}">'
                f'{esc(short_sheet(sheet))}</text>')
            add(f'<text class="t-colsub" x="{gx[ci]:.0f}" y="{y + 13:.0f}">'
                f'{esc(boards[board])}</text>')

    for w in wires:
        tip = f'{w["net"]}  ({len(w["pins"])} pins)\n' + ", ".join(
            f'{p["des"]}.{p["pin"]}' for p in w["pins"])
        add(f'<g class="netg" data-net="{esc(w["net"])}"><title>{esc(tip)}</title>'
            f'<path class="wire {w["domain"]}" d="{w["d"]}"/></g>')

    for g in glyphs:
        add(f'<g class="netg" data-net="{esc(g["net"])}">'
            f'<title>{esc(g["net"])}  {esc(g["des"])}.{esc(g["pin"])}</title>'
            f'{glyph_svg(g)}</g>')

    for s in stubs:
        dx, dy = s["dir"]
        ex, ey = s["x"] + dx * 12, s["y"] + dy * 12
        anchor = "middle" if dx == 0 else ("start" if dx > 0 else "end")
        add(f'<g class="netg" data-net="{esc(s["net"])}">'
            f'<line class="wire {s["domain"]}" x1="{s["x"]:.1f}" y1="{s["y"]:.1f}" '
            f'x2="{ex:.1f}" y2="{ey:.1f}"/>'
            f'<text class="t-net" x="{ex + dx * 3:.1f}" y="{ey + 3:.1f}" '
            f'text-anchor="{anchor}">{esc(s["net"])}</text></g>')

    for key, rec in placed.items():
        sym, comp = rec["sym"], rec["comp"]
        body, (ox, oy) = sym.draw(rec["x"], rec["y"])
        add(f'<g class="comp"><title>{esc(comp["designator"])}  '
            f'{esc(comp["comment"])}  [{esc(comp["lib_ref"])}]  '
            f'{esc(short_sheet(comp["sheet"]))}</title>')
        add(body)
        for p in sym.pins:
            ax, ay = p["anchor"][0] + ox, p["anchor"][1] + oy
            rx, ry = p["root"][0] + ox, p["root"][1] + oy
            add(f'<line class="pinline" x1="{rx:.1f}" y1="{ry:.1f}" '
                f'x2="{ax:.1f}" y2="{ay:.1f}"/>')
        add(f'<text class="t-des" x="{rec["x"]:.1f}" y="{rec["y"] - 5:.1f}">'
            f'{esc(comp["designator"])}</text>')
        for i, lk in enumerate(links.get(key, [])):
            # this connector is a board boundary: say which link it is and
            # where the other half lives
            label = f'{lk["link"]} → {lk["peer_board"].replace(" Board", "")}'
            bw = 6.2 * len(label) + 12
            by = rec["y"] + sym.h + 16 + i * 15
            add(f'<g class="linkbadge"><title>{esc(lk["link"])}: '
                f'{esc(comp["designator"])} to {esc(lk["peer_connector"])} on '
                f'{esc(lk["peer_board"])}, {lk["wires"]} wires</title>'
                f'<rect x="{rec["x"] - 2:.1f}" y="{by - 9:.1f}" width="{bw:.0f}" '
                f'height="13" rx="6.5"/>'
                f'<text x="{rec["x"] + bw / 2 - 2:.1f}" y="{by + 0.5:.1f}">'
                f'{esc(label)}</text></g>')
        if comp["comment"]:
            add(f'<text class="t-val" x="{rec["x"]:.1f}" '
                f'y="{rec["y"] + sym.h + 11:.1f}">{esc(comp["comment"])}</text>')
        add("</g>")

    add("</svg>")
    return "\n".join(out)


CSS = """
:root{
  --bg:#f8fafc;--page:#fffdf7;--ink:#0f1720;--muted:#5a6a7a;--line:#d3dce6;
  --sym:#1b3a6b;--wire:#123d8c;--sig:#123d8c;--pwr:#b3450c;--gnd:#4a5a6a;
  --local:#93a2b3;--accent:#1b4fd8;--hi:#d92d20;--bandbg:#1b4fd8;
  --b0:#0f766e;--b1:#7c3aed;--b2:#b45309;--cross:#be123c;
  --sans:"IBM Plex Sans",ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0d1219;--page:#131a23;--ink:#dfe8f2;--muted:#8ba0b5;--line:#26313d;
  --sym:#9dc0f0;--wire:#6ea8fe;--sig:#6ea8fe;--pwr:#fb923c;--gnd:#94a3b8;
  --local:#4d5c6e;--accent:#6ea8fe;--hi:#ff7b72;--bandbg:#6ea8fe;
  --b0:#2dd4bf;--b1:#c084fc;--b2:#fbbf24;--cross:#fb7185;}}
:root[data-theme="dark"]{
  --bg:#0d1219;--page:#131a23;--ink:#dfe8f2;--muted:#8ba0b5;--line:#26313d;
  --sym:#9dc0f0;--wire:#6ea8fe;--sig:#6ea8fe;--pwr:#fb923c;--gnd:#94a3b8;
  --local:#4d5c6e;--accent:#6ea8fe;--hi:#ff7b72;--bandbg:#6ea8fe;
  --b0:#2dd4bf;--b1:#c084fc;--b2:#fbbf24;--cross:#fb7185;}
body{background:var(--bg);color:var(--ink);font:14px/1.55 var(--sans);}
.wrap{max-width:1760px;margin:0 auto;padding:26px 22px 60px;}
h1{font-size:24px;font-weight:600;margin:0 0 4px;}
.sub{color:var(--muted);font-size:13px;margin:0 0 16px;max-width:74ch;}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 14px;}
.kpi{background:var(--page);border:1px solid var(--line);border-radius:9px;
  padding:8px 13px;min-width:92px;}
.kpi .v{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;}
.kpi .l{font-size:10px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.1em;font-weight:500;}
.readout{position:sticky;top:0;z-index:5;background:var(--bg);padding:7px 0;
  font-family:var(--mono);font-size:12px;color:var(--muted);
  border-bottom:1px solid var(--line);}
.readout b{color:var(--hi);}
.figure{background:var(--page);border:1px solid var(--line);border-radius:11px;
  padding:8px;overflow:auto;max-height:84vh;margin:10px 0 0;}
svg.eq{display:block;width:100%;height:auto;min-width:1600px;}
.page{fill:var(--page);}
.band{fill:var(--bandbg);fill-opacity:.045;stroke:var(--bandbg);
  stroke-opacity:.22;stroke-width:1;}
.boardzone{fill-opacity:.06;stroke-width:1.3;stroke-opacity:.5;stroke-dasharray:7 5;}
.boardzone.t0{fill:var(--b0);stroke:var(--b0);}
.boardzone.t1{fill:var(--b1);stroke:var(--b1);}
.boardzone.t2{fill:var(--b2);stroke:var(--b2);}
.t-board{font-size:13px;font-weight:700;letter-spacing:.03em;}
.t-board.t0{fill:var(--b0);} .t-board.t1{fill:var(--b1);} .t-board.t2{fill:var(--b2);}
.wire.cross{stroke:var(--cross);stroke-width:1.9;opacity:.95;}
.linkbadge rect{fill:var(--cross);fill-opacity:.14;stroke:var(--cross);
  stroke-width:.9;}
.linkbadge text{font-size:8px;font-weight:700;fill:var(--cross);
  font-family:var(--mono);text-anchor:middle;}
.linkbadge{cursor:help;}
.sym{fill:none;stroke:var(--sym);stroke-width:1.15;}
.fillsym{fill:var(--sym);fill-opacity:.15;}
.pinline{stroke:var(--sym);stroke-width:1.1;}
.wire{fill:none;stroke:var(--wire);stroke-width:1.25;opacity:.85;}
.wire.local{stroke:var(--local);}
.wire.pwr{stroke:var(--pwr);} .wire.gnd{stroke:var(--gnd);}
.rail{stroke-width:1.2;fill:none;} .rail.pwr{stroke:var(--pwr);}
.rail.gnd{stroke:var(--gnd);}
text{font-family:var(--sans);}
.t-col{font-size:12px;font-weight:700;fill:var(--ink);}
.t-colsub{font-size:9px;fill:var(--muted);text-transform:uppercase;
  letter-spacing:.08em;}
.t-band{font-size:10px;font-weight:700;fill:var(--bandbg);letter-spacing:.14em;}
.t-des{font-size:8.5px;font-weight:600;fill:var(--ink);font-family:var(--mono);}
.t-val{font-size:7.5px;fill:var(--muted);font-family:var(--mono);}
.t-net{font-size:7.5px;fill:var(--wire);font-family:var(--mono);}
.t-rail{font-size:7px;font-family:var(--mono);} .t-rail.pwr{fill:var(--pwr);}
.netg{cursor:crosshair;}
.netg.hot .wire,.netg.hot .rail{stroke:var(--hi);stroke-width:2.8;opacity:1;}
.netg.hot text{fill:var(--hi);font-weight:700;}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 6px;font-size:12px;
  color:var(--muted);}
.legend i{display:inline-block;width:22px;height:3px;border-radius:2px;
  vertical-align:middle;margin-right:6px;}
.legend .w-cross{background:var(--cross);} .legend .w-sig{background:var(--sig);} .legend .w-local{background:var(--local);}
.legend .w-pwr{background:var(--pwr);} .legend .w-gnd{background:var(--gnd);}
.note{color:var(--muted);font-size:12px;margin:14px 0 0;}
.note code{font-family:var(--mono);font-size:11.5px;}
"""

JS = """
(function(){
  var r=document.getElementById('readout');
  function set(n){
    document.querySelectorAll('.netg.hot').forEach(function(e){e.classList.remove('hot');});
    if(!n){r.innerHTML='Hover any wire or power symbol to trace a net across the whole system.';return;}
    var q=document.querySelectorAll('.netg[data-net="'+CSS.escape(n)+'"]');
    q.forEach(function(e){e.classList.add('hot');});
    r.innerHTML='net <b>'+n+'</b> &middot; '+q.length+' place(s) on this drawing';
  }
  document.addEventListener('mouseover',function(ev){
    var g=ev.target.closest?ev.target.closest('.netg'):null;
    if(g&&g.dataset.net) set(g.dataset.net);
  });
  set(null);
})();
"""


def main(argv: list[str]) -> int:
    argv, ref = project.take_project_arg(list(argv))
    project.use(ref)
    keep_all = "--all-passives" in argv
    fanout = RAIL_FANOUT
    if "--fanout" in argv:
        fanout = int(argv[argv.index("--fanout") + 1])

    model = model_io.read(P.out / "system_model.json")
    set_sheet_names([blk["sheet"] for b in model["boards"] for blk in b["blocks"]])
    nets = merge_nets(model)
    symbols = load_symbols()
    kept, dropped, rails, node_nets = select(model, nets, keep_all)
    lay = layout(model, kept, symbols, nets, node_nets)
    wires, glyphs, stubs, stats = route(lay, nets, rails, fanout)
    links = link_index(model)
    svg = render(model, lay, wires, glyphs, stubs, dropped, links)

    total_parts = sum(b["component_count"] for b in model["boards"])
    name = P.spec["title"] if P.spec else model["system"]["name"]
    h = [f'<title>{esc(name)} Equivalent Circuit</title>',
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Mono:wght@400;500;600&'
         'family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">',
         f"<style>{CSS}</style>", '<div class="wrap">',
         f"<h1>{esc(name)} &mdash; equivalent circuit</h1>",
         '<p class="sub">All three boards as one circuit: every net is merged '
         'across the board-to-board connectors, so a signal runs from the MCU '
         'pin to the MOSFET gate as a single wire. Symbols are the design\'s own. '
         'Decoupling is collapsed and supply nets are drawn as power symbols &mdash; '
         'see the note below the drawing.</p>',
         '<div class="kpis">']
    for label, value in (("Parts drawn", len(lay["placed"])),
                         ("Of total", total_parts),
                         ("Merged nets", len(nets)),
                         ("Drawn as wire", stats["wire"]),
                         ("Drawn as rail", stats["rail"]),
                         ("Cross boards", stats["crossing"])):
        h.append(f'<div class="kpi"><div class="v">{value}</div>'
                 f'<div class="l">{label}</div></div>')
    h.append("</div>")
    h.append('<div class="legend">'
             '<span><i class="w-cross"></i>net that crosses a board boundary</span>'
             '<span><i class="w-sig"></i>signal net inside one board</span>'
             '<span><i class="w-local"></i>unnamed local net</span>'
             '<span><i class="w-pwr"></i>supply rail, drawn as a power symbol '
             'at each pin</span>'
             '<span><i class="w-gnd"></i>ground</span>'
             '<span>dashed zones = the three PCBs</span>'
             '<span>C_n badge = the connector pair joining two boards</span>'
             '<span>bands = the three phase channels, drawn in full</span>'
             '</div>')
    h.append('<div class="readout" id="readout"></div>')
    h.append(f'<div class="figure">{svg}</div>')

    agg = Counter()
    for (board, sheet, kind), n in dropped.items():
        agg[kind] += n
    h.append('<p class="note"><b>What was reduced.</b> '
             f'{agg.get("decoupling", 0)} decoupling/bypass parts (a two-pin '
             'passive whose both nets are supply rails) and '
             f'{agg.get("testpoint", 0) + agg.get("mechanical", 0)} test points '
             'and mounting holes are not drawn; they carry no topology. Series '
             'passives &mdash; gate resistors, shunts, filter elements &mdash; are kept. '
             f'Nets with more than {fanout} pins, and every supply or ground net, '
             'are drawn as power symbols at each pin rather than as wire. '
             'Run <code>python render_equivalent.py --all-passives</code> to draw '
             'everything.</p>')
    h.append("</div>")
    h.append(f"<script>{JS}</script>")
    out = P.out / "equivalent_circuit.html"
    atomic_write_text(out, "\n".join(h))
    print(f"wrote {out.name}  ({out.stat().st_size:,} bytes)")
    print(f"  {len(lay['placed'])} of {total_parts} parts drawn, "
          f"{len(nets)} merged nets: {stats['wire']} as wire, "
          f"{stats['rail']} as rail, {stats['stub']} as stub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
