#!/usr/bin/env python3
"""
Render system_model.json as a single reviewable HTML page with an SVG block
diagram of the whole multi-board controller.

Layout is deterministic: one column per board in model order, external
interfaces in side columns, and block rows ordered by a barycentre sweep so
the connection lines cross as little as possible without any hand placement.

Run:  python render_system.py
Out:  system_diagram.html
"""

from __future__ import annotations

import sys

import html
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import model_io
import project                              # noqa: E402
from project import P                       # noqa: E402
from freshness import atomic_write_text      # noqa: E402

# --- geometry -------------------------------------------------------------
# A board container is wider than its blocks: the extra room on the left is a
# routing gutter, so sheet-to-sheet links loop inside the board instead of
# cutting across the lane the board-to-board links need.
EXT_W, BOARD_W, BLOCK_W = 170, 314, 228
GUTTER = 66
COL_GAP = 156
PAD_X, PAD_Y = 20, 44
ROW_GAP = 26
LINE_H = 15
TOP = 96
MARGIN = 30

DOMAIN_COLOR = {
    "power": "var(--c-power)",
    "ground": "var(--c-gnd)",
    "signal": "var(--c-signal)",
    "local": "var(--c-local)",
    "mixed": "var(--c-mixed)",
}


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


def dominant_domain(by_domain: dict) -> str:
    if not by_domain:
        return "signal"
    if len([k for k in by_domain if k in ("power", "ground", "signal")]) > 1:
        return "mixed"
    return max(by_domain, key=by_domain.get)


# --------------------------------------------------------------------------
# node model
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("id", "col", "x", "w", "y", "h", "title", "sub", "lines",
                 "badge", "kind", "order")

    def __init__(self, nid, col, title, sub, lines, badge=None, kind="block"):
        self.id, self.col = nid, col
        self.title, self.sub, self.lines = title, sub, lines
        self.badge, self.kind = badge, kind
        self.x = self.w = self.y = self.h = 0
        self.order = 0.0

    def height(self) -> int:
        base = 30 if self.kind == "block" else 24
        n = len(self.lines) + (1 if self.sub else 0)
        return base + n * LINE_H + 12

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def build_graph(model: dict):
    boards = model["boards"]
    n_boards = len(boards)
    nodes: dict[str, Node] = {}
    edges = []

    # external interface columns: first board's edges on the left, last
    # board's on the right; anything in between is tagged inside its block.
    ext_left_board = boards[0]["key"] if n_boards else None
    ext_right_board = boards[-1]["key"] if n_boards else None

    for bi, b in enumerate(boards):
        for blk in b["blocks"]:
            lines = [f"{kp['designator']}  {kp['comment']}" for kp in blk["key_parts"][:7]]
            extra = len(blk["key_parts"]) - 7
            if extra > 0:
                lines.append(f"+{extra} more")
            if not lines:
                lines = [f"{blk['component_count']} passives / mechanical"]
            badge = f"x{len(blk['rooms'])}" if blk["rooms"] else None
            nid = f"{b['key']}::{blk['sheet']}"
            nodes[nid] = Node(nid, bi + 1, short_sheet(blk["sheet"]),
                              f"{blk['component_count']} parts", lines, badge)

        for bl in b["block_links"]:
            edges.append({
                "a": f"{b['key']}::{bl['a']}", "b": f"{b['key']}::{bl['b']}",
                "count": bl["net_count"], "domain": "signal",
                "label": str(bl["net_count"]),
                "nets": bl["nets"], "scope": "intra",
            })

        # external interfaces, grouped by logical designator (3 phase lugs
        # become one node rather than three identical ones)
        groups: dict[str, list] = defaultdict(list)
        for e in b.get("external_interfaces", []):
            groups[e["logical_designator"]].append(e)
        for logical, items in sorted(groups.items()):
            if b["key"] == ext_left_board:
                col = 0
            elif b["key"] == ext_right_board:
                col = n_boards + 1
            else:
                continue
            nets = sorted({n for it in items for n in it["nets"]})
            lines = [n for n in nets[:5]]
            if len(nets) > 5:
                lines.append(f"+{len(nets) - 5} nets")
            nid = f"ext::{b['key']}::{logical}"
            badge = f"x{len(items)}" if len(items) > 1 else None
            nodes[nid] = Node(nid, col, logical, items[0]["comment"], lines,
                              badge, kind="ext")
            edges.append({
                "a": nid, "b": f"{b['key']}::{items[0]['sheet']}",
                "count": sum(it["pin_count"] for it in items),
                "domain": "mixed", "label": "",
                "nets": nets, "scope": "ext",
            })

    key_to_title = {b["key"]: b["title"] for b in boards}
    title_to_key = {v: k for k, v in key_to_title.items()}
    for lk in model["interboard_links"]:
        ak, bk = title_to_key.get(lk["a_board"]), title_to_key.get(lk["b_board"])
        if not (ak and bk and lk["a_block"] and lk["b_block"]):
            continue
        edges.append({
            "a": f"{ak}::{lk['a_block']}", "b": f"{bk}::{lk['b_block']}",
            "count": lk["wire_count"], "domain": dominant_domain(lk["by_domain"]),
            "label": lk["designator"], "nets": [n for n in lk["nets"] if n],
            "scope": "inter",
            "detail": f"{lk['a_connector']} ↔ {lk['b_connector']}",
        })

    edges = [e for e in edges if e["a"] in nodes and e["b"] in nodes]
    return nodes, edges


def layout(nodes: dict[str, Node], n_cols: int):
    """Column x, then a barycentre sweep to pick the row order in each column."""
    cols: dict[int, list[Node]] = defaultdict(list)
    for n in nodes.values():
        cols[n.col].append(n)

    x = MARGIN
    col_x = {}
    for c in range(n_cols):
        width = EXT_W if c in (0, n_cols - 1) else BOARD_W
        col_x[c] = x
        x += width + COL_GAP
    total_w = x - COL_GAP + MARGIN

    for c, members in cols.items():
        is_board = c not in (0, n_cols - 1)
        inner = BLOCK_W if is_board else EXT_W
        for n in members:
            n.w = inner
            n.x = col_x[c] + (GUTTER if is_board else 0)
            n.h = n.height()

    for c in sorted(cols):
        for i, n in enumerate(sorted(cols[c], key=lambda z: z.title)):
            n.order = float(i)

    return cols, col_x, total_w


def sweep(cols, edges, nodes, rounds: int = 14):
    """Barycentre ordering: each node drifts toward the mean row of its neighbours."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["a"]].append(e["b"])
        adj[e["b"]].append(e["a"])

    order = sorted(cols)
    for r in range(rounds):
        seq = order if r % 2 == 0 else list(reversed(order))
        for c in seq:
            members = cols[c]
            for n in members:
                nb = [nodes[m].order for m in adj[n.id] if nodes[m].col != c]
                if nb:
                    n.order = sum(nb) / len(nb)
            for i, n in enumerate(sorted(members, key=lambda z: (z.order, z.title))):
                n.order = float(i)


def assign_y(cols):
    """Stack each column, then centre the shorter columns against the tallest."""
    heights = {}
    for c, members in cols.items():
        members.sort(key=lambda z: z.order)
        heights[c] = sum(m.h for m in members) + ROW_GAP * (len(members) - 1)
    tallest = max(heights.values()) if heights else 0
    for c, members in cols.items():
        y = TOP + (tallest - heights[c]) / 2
        for m in members:
            m.y = y
            y += m.h + ROW_GAP
    return TOP + tallest + 60


# --------------------------------------------------------------------------
# svg
# --------------------------------------------------------------------------

def anchor_edges(nodes: dict[str, Node], edges: list[dict]):
    """
    Spread each node's connections along its edge instead of stacking them all
    on the centre point.

    Without this the repeated phase channels hide each other: six Power<->Driver
    links land on the same two block centres and draw as one curve with one
    label, so a reader sees three connections where the design has twelve.
    """
    side: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(edges):
        a, b = nodes[e["a"]], nodes[e["b"]]
        if a.col == b.col:
            side[(a.id, "L")].append(i)
            side[(b.id, "L")].append(i)
        else:
            left, right = (a, b) if a.x < b.x else (b, a)
            side[(left.id, "R")].append(i)
            side[(right.id, "L")].append(i)

    anchor: dict[tuple[int, str], float] = {}
    for (nid, s), idxs in side.items():
        n = nodes[nid]
        def other_cy(i: int) -> float:
            e = edges[i]
            o = nodes[e["b"] if e["a"] == nid else e["a"]]
            return o.cy
        idxs.sort(key=other_cy)
        span = max(n.h - 16, 8)
        for k, i in enumerate(idxs):
            frac = (k + 1) / (len(idxs) + 1)
            anchor[(i, nid)] = n.y + 8 + span * frac
    return anchor


def edge_path(a: Node, b: Node, y1: float, y2: float, lane: int = 0):
    if a.col == b.col:  # same board: loop left, into the gutter
        x = a.x
        bulge = 16 + (lane % 3) * 17
        d = f"M {x} {y1:.1f} C {x - bulge} {y1:.1f}, {x - bulge} {y2:.1f}, {x} {y2:.1f}"
        return d, x - bulge * 0.74, (y1 + y2) / 2
    left, right = (a, b) if a.x < b.x else (b, a)
    ly, ry = (y1, y2) if a.x < b.x else (y2, y1)
    x1, x2 = left.x + left.w, right.x
    mx = (x1 + x2) / 2
    d = f"M {x1} {ly:.1f} C {mx} {ly:.1f}, {mx} {ry:.1f}, {x2} {ry:.1f}"
    return d, mx, (ly + ry) / 2


def render_svg(model: dict) -> str:
    nodes, edges = build_graph(model)
    n_cols = len(model["boards"]) + 2
    cols, col_x, total_w = layout(nodes, n_cols)
    sweep(cols, edges, nodes)
    total_h = assign_y(cols)

    out: list[str] = []
    add = out.append
    add(f'<svg viewBox="0 0 {int(total_w)} {int(total_h)}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Multi-board controller system diagram">')
    add('<defs>'
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker>'
        '</defs>')

    # Board containers all span the same rows, so the three boards read as
    # lanes; a board with one block would otherwise float in empty space.
    board_cols = [c for c in cols if 0 < c < n_cols - 1 and cols[c]]
    lane_top = min(m.y for c in board_cols for m in cols[c]) - PAD_Y
    lane_bot = max(m.y + m.h for c in board_cols for m in cols[c]) + PAD_X
    for bi, b in enumerate(model["boards"]):
        members = cols.get(bi + 1, [])
        if not members:
            continue
        x = col_x[bi + 1]
        y, h = lane_top, lane_bot - lane_top
        add(f'<rect class="board" x="{x}" y="{y:.0f}" width="{BOARD_W}" '
            f'height="{h:.0f}" rx="14"/>')
        add(f'<text class="board-t" x="{x + PAD_X}" y="{y + 24:.0f}">'
            f'{esc(b["title"])}</text>')
        add(f'<text class="board-s" x="{x + PAD_X}" y="{y + 38:.0f}">'
            f'{b["component_count"]} parts &#183; {b["net_count"]} nets &#183; '
            f'{b["sheet_count"]} sheet(s)</text>')

    # edges first so boxes sit on top
    anchor = anchor_edges(nodes, edges)
    lanes: dict[int, int] = defaultdict(int)
    par_count: dict[tuple, int] = defaultdict(int)
    par_total: dict[tuple, int] = defaultdict(int)
    for e in edges:
        par_total[tuple(sorted((e["a"], e["b"])))] += 1
    for i, e in enumerate(edges):
        a, b = nodes[e["a"]], nodes[e["b"]]
        lane = 0
        if a.col == b.col:
            lane = lanes[a.col]
            lanes[a.col] += 1
        ya = anchor.get((i, a.id), a.cy)
        yb = anchor.get((i, b.id), b.cy)
        d, lx, ly = edge_path(a, b, ya, yb, lane)
        color = DOMAIN_COLOR.get(e["domain"], "var(--c-signal)")
        width = 1.4 if e["scope"] == "intra" else (2.6 if e["scope"] == "inter" else 1.6)
        dash = ' stroke-dasharray="5 4"' if e["scope"] == "ext" else ""
        nets = ", ".join(e["nets"][:18]) + (" ..." if len(e["nets"]) > 18 else "")
        tip = f'{e.get("detail", "")}  {e["count"]} wire(s)\n{nets}'.strip()
        add(f'<g class="edge" data-scope="{e["scope"]}">')
        add(f'<title>{esc(tip)}</title>')
        add(f'<path class="wire" d="{d}" stroke="{color}" stroke-width="{width}"{dash}/>')
        if e["scope"] != "ext":
            label = e["label"] or str(e["count"])
            txt = f'{label}' if e["scope"] == "intra" else f'{label} ({e["count"]}w)'
            w = 7 * len(txt) + 10
            # parallel links between the same two blocks: stagger the labels
            # sideways so six phase channels do not stack into one smear
            pair = tuple(sorted((a.id, b.id)))
            k = par_count[pair]
            par_count[pair] += 1
            if e["scope"] == "inter":
                lx += (k - (par_total[pair] - 1) / 2) * (w * 0.55)
            add(f'<rect class="lbl-bg" x="{lx - w / 2:.0f}" y="{ly - 9:.0f}" '
                f'width="{w}" height="17" rx="8"/>')
            add(f'<text class="lbl" x="{lx:.0f}" y="{ly + 3.5:.0f}">{esc(txt)}</text>')
        add('</g>')

    # nodes
    for n in nodes.values():
        cls = "node ext" if n.kind == "ext" else "node"
        add(f'<g class="{cls}">')
        add(f'<rect x="{n.x:.0f}" y="{n.y:.0f}" width="{n.w}" height="{n.h}" rx="9"/>')
        ty = n.y + (19 if n.kind == "block" else 16)
        add(f'<text class="n-t" x="{n.x + 11:.0f}" y="{ty:.0f}">{esc(n.title)}</text>')
        if n.badge:
            bw = 8 * len(n.badge) + 10
            add(f'<rect class="badge" x="{n.x + n.w - bw - 9:.0f}" '
                f'y="{n.y + 7:.0f}" width="{bw}" height="16" rx="8"/>')
            add(f'<text class="badge-t" x="{n.x + n.w - bw / 2 - 9:.0f}" '
                f'y="{n.y + 18.5:.0f}">{esc(n.badge)}</text>')
        y = ty + 14
        if n.sub:
            add(f'<text class="n-s" x="{n.x + 11:.0f}" y="{y:.0f}">{esc(n.sub)}</text>')
            y += LINE_H
        for line in n.lines:
            add(f'<text class="n-l" x="{n.x + 11:.0f}" y="{y:.0f}">{esc(line)}</text>')
            y += LINE_H
        add('</g>')

    add('</svg>')
    return "\n".join(out)


FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Mono:wght@400;500;600&'
         'family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">')

CSS = """
/* Neutrals carry a blue bias toward the signal accent rather than sitting on
   a flat grey, and both faces are IBM Plex: drawn for technical documentation,
   and the mono is doing real work here on designators and net names. */
:root{
  --bg:#f8fafc; --panel:#ffffff; --ink:#0f1720; --muted:#5a6a7a;
  --line:#d3dce6; --board:#eef2f7; --board-line:#bfccdb;
  --c-power:#c2410c; --c-gnd:#64748b; --c-signal:#1b4fd8;
  --c-local:#9fb0c2; --c-mixed:#7c3aed; --accent:#1b4fd8;
  --warn:#b45309; --err:#b91c1c;
  --sans:"IBM Plex Sans",ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0d1219; --panel:#141b24; --ink:#dfe8f2; --muted:#8ba0b5;
    --line:#26313d; --board:#121922; --board-line:#2c3a48;
    --c-power:#fb923c; --c-gnd:#94a3b8; --c-signal:#6ea8fe;
    --c-local:#47566a; --c-mixed:#c084fc; --accent:#6ea8fe;
    --warn:#fbbf24; --err:#f87171;
  }
}
:root[data-theme="dark"]{
  --bg:#0d1219; --panel:#141b24; --ink:#dfe8f2; --muted:#8ba0b5;
  --line:#26313d; --board:#121922; --board-line:#2c3a48;
  --c-power:#fb923c; --c-gnd:#94a3b8; --c-signal:#6ea8fe;
  --c-local:#47566a; --c-mixed:#c084fc; --accent:#6ea8fe;
  --warn:#fbbf24; --err:#f87171;
}
body{background:var(--bg);color:var(--ink);
  font:14px/1.55 var(--sans);}
a:focus-visible,tr:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.wrap{max-width:1720px;margin:0 auto;padding:28px 24px 80px;
  display:flex;flex-direction:column;gap:0;}
h1{font-size:25px;font-weight:600;letter-spacing:-.01em;margin:0 0 4px;
  text-wrap:balance;}
h2{font-size:17px;font-weight:600;margin:34px 0 12px;padding-bottom:6px;
  border-bottom:1px solid var(--line);}
h3{font-size:11px;margin:22px 0 8px;color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.11em;}
.sub{color:var(--muted);margin:0 0 20px;font-size:13px;max-width:68ch;}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px;}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:9px 14px;min-width:96px;}
.kpi .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;}
.kpi .l{font-size:10px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.1em;font-weight:500;}
.figure{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:10px;overflow-x:auto;}
svg{display:block;min-width:1180px;width:100%;height:auto;}
text{font-family:var(--sans);}
.board{fill:var(--board);stroke:var(--board-line);stroke-width:1.2;}
.board-t{font-size:14px;font-weight:700;fill:var(--ink);}
.board-s{font-size:10.5px;fill:var(--muted);}
.node rect{fill:var(--panel);stroke:var(--line);stroke-width:1.2;}
.node.ext rect{fill:none;stroke:var(--muted);stroke-dasharray:4 3;}
.n-t{font-size:12px;font-weight:650;fill:var(--ink);}
.n-s{font-size:10px;fill:var(--muted);}
.n-l{font-size:10.5px;fill:var(--muted);font-family:var(--mono);}
.badge{fill:var(--accent);opacity:.16;}
.badge-t{font-size:10px;font-weight:700;fill:var(--accent);text-anchor:middle;}
.wire{fill:none;opacity:.75;}
.lbl-bg{fill:var(--panel);stroke:var(--line);stroke-width:1;}
.lbl{font-size:10px;fill:var(--ink);text-anchor:middle;font-weight:600;}
.edge:hover .wire{opacity:1;stroke-width:4;}
.edge{cursor:help;}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:12px 2px 0;
  font-size:12px;color:var(--muted);}
.legend i{display:inline-block;width:22px;height:3px;border-radius:2px;
  vertical-align:middle;margin-right:6px;}
.tablewrap{overflow-x:auto;}
table{border-collapse:collapse;width:100%;font-size:12.5px;min-width:560px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;
  font-variant-numeric:tabular-nums;}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
  vertical-align:top;}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);font-weight:600;white-space:nowrap;}
tr:last-child td{border-bottom:none;}
td.mono,code{font-family:var(--mono);font-size:11.5px;}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;
  border:1px solid var(--line);margin:1px 3px 1px 0;}
.issue{border-left:3px solid var(--err);background:var(--panel);
  border-radius:0 8px 8px 0;padding:9px 13px;margin:0 0 8px;}
.issue.warning{border-left-color:var(--warn);}
.issue b{font-size:11px;text-transform:uppercase;letter-spacing:.07em;}
.ok{color:var(--muted);}
"""


def render_html(model: dict) -> str:
    boards = model["boards"]
    parts = sum(b["component_count"] for b in boards)
    nets = sum(b["net_count"] for b in boards)
    wires = sum(lk["wire_count"] for lk in model["interboard_links"])
    issues = [(b["title"], i) for b in boards for i in b["issues"]]

    h: list[str] = []
    a = h.append
    name = P.spec["title"] if P.spec else model["system"]["name"]
    a(f"<title>{esc(name)} System Map</title>")
    a(FONTS)
    a(f"<style>{CSS}</style>")
    a('<div class="wrap">')
    a(f"<h1>{esc(model['system']['name'])}</h1>")
    a(f'<p class="sub">Generated {esc(model["generated_utc"])} from '
      f'<code>{esc(model["source_mbs"])}</code> and the per-board Altium '
      f'extraction. Hover any connection for its net list.</p>')

    a('<div class="kpis">')
    for label, value in (("Boards", len(boards)), ("Components", parts),
                         ("Nets", nets), ("Board-to-board links",
                                          len(model["interboard_links"])),
                         ("Inter-board wires", wires),
                         ("Flagged", len(issues))):
        a(f'<div class="kpi"><div class="v">{value}</div>'
          f'<div class="l">{label}</div></div>')
    a("</div>")

    a('<div class="figure">')
    a(render_svg(model))
    a("</div>")
    a('<div class="legend">'
      '<span><i style="background:var(--c-signal)"></i>signal</span>'
      '<span><i style="background:var(--c-power)"></i>power</span>'
      '<span><i style="background:var(--c-gnd)"></i>ground</span>'
      '<span><i style="background:var(--c-mixed)"></i>mixed bundle</span>'
      '<span><i style="background:var(--muted)"></i>dashed = system edge '
      '(external connector)</span>'
      '<span>&#215;N badge = repeated channel</span></div>')

    a("<h2>Board-to-board links</h2>")
    a('<div class="tablewrap">'); a("<table><tr><th>Link</th><th>From</th><th>To</th><th>Wires</th>"
      "<th>Mix</th><th>Nets</th></tr>")
    for lk in model["interboard_links"]:
        dom = " ".join(f'<span class="tag">{esc(k)}&#183;{v}</span>'
                       for k, v in sorted(lk["by_domain"].items()))
        a(f'<tr><td class="mono">{esc(lk["designator"])}</td>'
          f'<td>{esc(lk["a_board"])}<br><code>{esc(lk["a_connector"])}</code></td>'
          f'<td>{esc(lk["b_board"])}<br><code>{esc(lk["b_connector"])}</code></td>'
          f'<td>{lk["wire_count"]}</td><td>{dom}</td>'
          f'<td class="mono">{esc(", ".join(n for n in lk["nets"] if n))}</td></tr>')
    a("</table></div>")

    for b in boards:
        a(f"<h2>{esc(b['title'])}</h2>")
        a("<h3>Functional blocks</h3>")
        a('<div class="tablewrap">'); a("<table><tr><th>Sheet</th><th>Parts</th><th>Channels</th>"
          "<th>Key devices</th><th>Connectors</th></tr>")
        for blk in b["blocks"]:
            key = "<br>".join(
                f'<code>{esc(k["designator"])}</code> {esc(k["comment"])}'
                for k in blk["key_parts"]) or '<span class="ok">-</span>'
            conn = " ".join(f'<span class="tag">{esc(c)}</span>'
                            for c in blk["connectors"]) or '<span class="ok">-</span>'
            rooms = (", ".join(blk["rooms"]) if blk["rooms"]
                     else '<span class="ok">-</span>')
            a(f'<tr><td class="mono">{esc(short_sheet(blk["sheet"]))}</td>'
              f'<td>{blk["component_count"]}</td><td>{rooms}</td>'
              f'<td>{key}</td><td>{conn}</td></tr>')
        a("</table></div>")

        if b["block_links"]:
            a("<h3>Signals between sheets</h3>")
            a('<div class="tablewrap">'); a("<table><tr><th>From</th><th>To</th><th>n</th><th>Nets</th></tr>")
            for bl in b["block_links"]:
                a(f'<tr><td class="mono">{esc(short_sheet(bl["a"]))}</td>'
                  f'<td class="mono">{esc(short_sheet(bl["b"]))}</td>'
                  f'<td>{bl["net_count"]}</td>'
                  f'<td class="mono">{esc(", ".join(bl["nets"]))}</td></tr>')
            a("</table></div>")

        rails = [r for r in b["power_rails"] if r["node_count"] >= 3]
        if rails:
            a("<h3>Power rails</h3>")
            a('<div class="tablewrap">'); a("<table><tr><th>Net</th><th>Domain</th><th>Pins</th>"
              "<th>Sheets</th><th>Likely source</th></tr>")
            for r in rails[:20]:
                a(f'<tr><td class="mono">{esc(r["net"])}</td>'
                  f'<td>{esc(r["domain"])}</td><td>{r["node_count"]}</td>'
                  f'<td>{len(r["sheets"])}</td>'
                  f'<td class="mono">{esc(", ".join(r["likely_sources"]) or "-")}</td></tr>')
            a("</table></div>")

        ext = b.get("external_interfaces") or []
        if ext:
            a("<h3>System edges</h3>")
            a('<div class="tablewrap">'); a("<table><tr><th>Connector</th><th>Part</th><th>Pins</th>"
              "<th>Sheet</th><th>Nets</th></tr>")
            for e in ext:
                a(f'<tr><td class="mono">{esc(e["designator"])}</td>'
                  f'<td>{esc(e["comment"])}</td><td>{e["pin_count"]}</td>'
                  f'<td class="mono">{esc(short_sheet(e["sheet"]))}</td>'
                  f'<td class="mono">{esc(", ".join(e["nets"]))}</td></tr>')
            a("</table></div>")

    a("<h2>Flagged for review</h2>")
    if not issues:
        a('<p class="ok">Nothing flagged.</p>')
    for title, i in sorted(issues, key=lambda x: x[1]["severity"]):
        a(f'<div class="issue {esc(i["severity"])}">'
          f'<b>{esc(i["severity"])} &#183; {esc(title)} &#183; {esc(i["kind"])}</b>'
          f'<div>{esc(i["where"])} &mdash; {esc(i["detail"])}</div></div>')

    a("</div>")
    return "\n".join(h)


def main(argv: list[str] | None = None) -> int:
    argv, ref = project.take_project_arg(list(argv if argv is not None else sys.argv[1:]))
    project.use(ref)
    model = model_io.read(P.out / "system_model.json")
    set_sheet_names([blk["sheet"] for b in model["boards"] for blk in b["blocks"]])
    out = P.out / "system_diagram.html"
    atomic_write_text(out, render_html(model))
    print(f"wrote {out.name} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
