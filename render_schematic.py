#!/usr/bin/env python3
"""
Redraw the actual schematic sheets from the .SchDoc files as SVG.

Symbol graphics are stored in absolute sheet coordinates, so the real Altium
symbols -- the MCU outline, the resistor and capacitor bodies, every pin with
its name -- can be drawn directly rather than substituted with stand-ins.

Each sheet is also wired to the offline netlist: hovering any wire, pin or
label highlights that whole net across the sheet and names it. That is the
part a reviewer actually uses, and it is also how you can see at a glance that
the netlister grouped things the way the drawing says.

Run:  python render_schematic.py [Board ...]
Out:  schematics/<Board>/<Sheet>.svg  and  schematics/index.html
"""

from __future__ import annotations

import html
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from altium_sch import PIN_DIR, REC, Sheet, parse_sheet, primitive_points, rint
import project
from boards import BOARDS, sheet_paths
from project import P
from netlist_offline import build_board

ROOT = Path(__file__).resolve().parent

SHOW_NAME = 0x08
SHOW_DESIGNATOR = 0x10


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


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


class Canvas:
    """Sheet coordinates in, SVG coordinates out. Altium's Y grows upward."""

    def __init__(self, bbox, margin: float = 30):
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.m = margin

    @property
    def width(self) -> float:
        return (self.x1 - self.x0) + 2 * self.m

    @property
    def height(self) -> float:
        return (self.y1 - self.y0) + 2 * self.m

    def x(self, v: float) -> float:
        return v - self.x0 + self.m

    def y(self, v: float) -> float:
        return self.y1 - v + self.m

    def pt(self, p) -> tuple[float, float]:
        return self.x(p[0]), self.y(p[1])


def sheet_bbox(sheet: Sheet):
    """
    Crop to what is actually drawn, not to the declared sheet size.

    The sheets here are A-size frames with the circuit occupying a corner, so
    using CustomX/CustomY would render each one as a postage stamp in a field
    of empty paper. Taking the real extent also survives objects placed outside
    the frame, which several of these sheets have.
    """
    xs, ys = [], []
    for w in sheet.wires:
        xs += [p[0] for p in w]
        ys += [p[1] for p in w]
    for c in sheet.components:
        xs.append(c.x)
        ys.append(c.y)
        for p in c.pins:
            xs += [p.x, p.end[0]]
            ys += [p.y, p.end[1]]
    for g in sheet.graphics:
        for px, py in primitive_points(g["raw"]):
            xs.append(px)
            ys.append(py)
    for coll in (sheet.net_labels, sheet.powers, sheet.ports):
        xs += [o["x"] for o in coll]
        ys += [o["y"] for o in coll]
    for s in sheet.sheet_symbols:
        xs += [s.x, s.x + s.xsize]
        ys += [s.y, s.y - s.ysize]
    if not xs:
        return (0.0, 0.0, 1000.0, 800.0)
    return (min(xs), min(ys), max(xs), max(ys))


def draw_graphic(g: dict, cv: Canvas) -> str:
    kv, kind = g["raw"], g["kind"]
    try:
        if kind == "rectangle":
            x1, y1 = cv.pt((float(kv["Location.X"]), float(kv["Location.Y"])))
            x2, y2 = cv.pt((float(kv["Corner.X"]), float(kv["Corner.Y"])))
            return (f'<rect class="sym" x="{min(x1, x2):.1f}" y="{min(y1, y2):.1f}" '
                    f'width="{abs(x2 - x1):.1f}" height="{abs(y2 - y1):.1f}"/>')
        if kind in ("polyline", "polygon", "line", "bezier"):
            if kind == "line":
                pts = [(float(kv["Location.X"]), float(kv["Location.Y"])),
                       (float(kv["Corner.X"]), float(kv["Corner.Y"]))]
            else:
                n = int(kv.get("LocationCount", 0) or 0)
                pts = [(float(kv[f"X{i}"]), float(kv[f"Y{i}"])) for i in range(1, n + 1)]
            if len(pts) < 2:
                return ""
            d = " ".join(f"{'M' if i == 0 else 'L'} {cv.x(p[0]):.1f} {cv.y(p[1]):.1f}"
                         for i, p in enumerate(pts))
            close = " Z" if kind == "polygon" else ""
            fill = ' fill-rule="evenodd"' if kind == "polygon" else ""
            return f'<path class="sym{" fillsym" if kind == "polygon" else ""}" d="{d}{close}"{fill}/>'
        if kind in ("arc", "elliptical_arc", "ellipse"):
            cx, cy = float(kv["Location.X"]), float(kv["Location.Y"])
            r = float(kv.get("Radius", kv.get("SecondaryRadius", 0)) or 0)
            if kind == "ellipse" or not kv.get("StartAngle"):
                rx = float(kv.get("Radius", r) or r)
                ry = float(kv.get("SecondaryRadius", rx) or rx)
                return (f'<ellipse class="sym" cx="{cv.x(cx):.1f}" cy="{cv.y(cy):.1f}" '
                        f'rx="{rx:.1f}" ry="{ry:.1f}"/>')
            a0 = math.radians(float(kv.get("StartAngle", 0) or 0))
            a1 = math.radians(float(kv.get("EndAngle", 0) or 0))
            sx, sy = cx + r * math.cos(a0), cy + r * math.sin(a0)
            ex, ey = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
            return (f'<path class="sym" d="M {cv.x(sx):.1f} {cv.y(sy):.1f} '
                    f'A {r:.1f} {r:.1f} 0 {large} 0 {cv.x(ex):.1f} {cv.y(ey):.1f}"/>')
    except (KeyError, ValueError):
        return ""
    return ""


def power_glyph(p: dict, cv: Canvas) -> str:
    """A small mark per power-port style, oriented the way it was placed."""
    x, y = cv.pt((p["x"], p["y"]))
    o = p["orientation"]           # 0 right, 1 up, 2 left, 3 down
    dx, dy = {0: (1, 0), 1: (0, -1), 2: (-1, 0), 3: (0, 1)}.get(o, (0, 1))
    ex, ey = x + dx * 10, y + dy * 10
    parts = [f'<line class="pwr" x1="{x:.1f}" y1="{y:.1f}" x2="{ex:.1f}" y2="{ey:.1f}"/>']
    px, py = -dy, dx
    if p["style"] in (2, 4, 6):      # ground-like: stacked bars
        for i, half in enumerate((7, 4.5, 2)):
            bx, by = x + dx * (10 + i * 3), y + dy * (10 + i * 3)
            parts.append(f'<line class="pwr" x1="{bx - px * half:.1f}" y1="{by - py * half:.1f}" '
                         f'x2="{bx + px * half:.1f}" y2="{by + py * half:.1f}"/>')
    else:                            # supply-like: a bar
        parts.append(f'<line class="pwr" x1="{ex - px * 7:.1f}" y1="{ey - py * 7:.1f}" '
                     f'x2="{ex + px * 7:.1f}" y2="{ey + py * 7:.1f}"/>')
    tx, ty = x + dx * 26, y + dy * 26
    anchor = "middle" if dx == 0 else ("start" if dx > 0 else "end")
    parts.append(f'<text class="t-pwr" x="{tx:.1f}" y="{ty + (3 if dx else 0):.1f}" '
                 f'text-anchor="{anchor}">{esc(p["text"])}</text>')
    return "".join(parts)


def render_sheet(sheet: Sheet, node_net: dict, title: str) -> str:
    cv = Canvas(sheet_bbox(sheet))
    W, H = cv.width, cv.height

    # net for every wire, via the pins that touch it -- good enough to colour
    # and to group for highlighting
    out: list[str] = []
    add = out.append
    add(f'<svg viewBox="0 0 {W:.0f} {H:.0f}" xmlns="http://www.w3.org/2000/svg" '
        f'class="sch" role="img" aria-label="{esc(title)}">')
    add(f'<rect class="page" x="0" y="0" width="{W:.0f}" height="{H:.0f}"/>')

    # wires
    for pts in sheet.wires:
        net = None
        for p in pts:
            net = net or node_net.get(("xy", p))
        d = " ".join(f"{'M' if i == 0 else 'L'} {cv.x(p[0]):.1f} {cv.y(p[1]):.1f}"
                     for i, p in enumerate(pts))
        cls = f"wire {domain(net)}" if net else "wire"
        title_el = f"<title>{esc(net)}</title>" if net else ""
        add(f'<g class="netg" data-net="{esc(net or "")}">{title_el}'
            f'<path class="{cls}" d="{d}"/></g>')

    for j in sheet.junctions:
        x, y = cv.pt(j)
        add(f'<circle class="junction" cx="{x:.1f}" cy="{y:.1f}" r="2.6"/>')

    # No-ERC crosses: the only mark that says an open pin is deliberate, so it
    # is drawn where Altium draws it rather than left out of the picture
    for d in sheet.no_erc:
        if not d["active"]:
            continue
        x, y = cv.pt((d["x"], d["y"]))
        r = 3.4
        add(f'<g class="noerc"><title>No ERC -- deliberately not connected</title>'
            f'<path d="M {x - r:.1f} {y - r:.1f} L {x + r:.1f} {y + r:.1f} '
            f'M {x - r:.1f} {y + r:.1f} L {x + r:.1f} {y - r:.1f}"/></g>')

    # sheet symbols
    for s in sheet.sheet_symbols:
        x, y = cv.pt((s.x, s.y))
        add(f'<rect class="sheetsym" x="{x:.1f}" y="{y:.1f}" '
            f'width="{s.xsize:.1f}" height="{s.ysize:.1f}"/>')
        add(f'<text class="t-sheet" x="{x + 8:.1f}" y="{y + 18:.1f}">{esc(s.name)}</text>')
        add(f'<text class="t-small" x="{x + 8:.1f}" y="{y + 32:.1f}">{esc(s.file_name)}</text>')
        for ent in s.entries:
            ex = s.x + (0 if ent["side"] == 0 else s.xsize)
            ey = s.y - ent["distance_from_top"] * 10
            px, py = cv.pt((ex, ey))
            add(f'<circle class="entry" cx="{px:.1f}" cy="{py:.1f}" r="2.6"/>')
            add(f'<text class="t-small" x="{px + 6:.1f}" y="{py + 3:.1f}">{esc(ent["name"])}</text>')

    # components
    graphics_by_owner: dict[int, list] = defaultdict(list)
    for g in sheet.graphics:
        graphics_by_owner[g["owner"] + 1].append(g)

    for comp in sheet.components:
        add(f'<g class="comp" data-des="{esc(comp.designator)}">')
        add(f'<title>{esc(comp.designator)}  {esc(comp.comment)}  '
            f'[{esc(comp.lib_ref)}]</title>')
        for g in graphics_by_owner.get(comp.index, []):
            if (g["part_id"] not in (-1, comp.part_id)
                    or g.get("display_mode", 0) != comp.display_mode):
                continue
            add(draw_graphic(g, cv))
        for pin in comp.pins:
            sx, sy = cv.pt((pin.x, pin.y))
            ex, ey = cv.pt(pin.end)
            net = node_net.get((comp.designator, pin.designator))
            add(f'<g class="netg" data-net="{esc(net or "")}">'
                f'<title>{esc(comp.designator)}.{esc(pin.designator)}'
                f'{"  " + esc(pin.name) if pin.name else ""}'
                f'{"  " + esc(net) if net else ""}</title>'
                f'<line class="pin {domain(net) if net else ""}" '
                f'x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}"/></g>')
            dx, dy = PIN_DIR.get(pin.conglomerate & 0x03, (1, 0))
            if pin.conglomerate & SHOW_NAME and pin.name not in ("", None):
                nx, ny = cv.pt((pin.x - dx * 4, pin.y - dy * 4))
                anchor = "middle" if dx == 0 else ("end" if dx > 0 else "start")
                add(f'<text class="t-pin" x="{nx:.1f}" y="{ny + 3:.1f}" '
                    f'text-anchor="{anchor}">{esc(pin.name)}</text>')
            if pin.conglomerate & SHOW_DESIGNATOR:
                mx, my = cv.pt((pin.x + dx * pin.length / 2, pin.y + dy * pin.length / 2))
                add(f'<text class="t-num" x="{mx:.1f}" y="{my - 3:.1f}" '
                    f'text-anchor="middle">{esc(pin.designator)}</text>')
        if comp.designator:
            dxp, dyp = cv.pt((comp.x, comp.y))
            add(f'<text class="t-des" x="{dxp:.1f}" y="{dyp - 6:.1f}">{esc(comp.designator)}</text>')
            if comp.comment:
                add(f'<text class="t-val" x="{dxp:.1f}" y="{dyp + 12:.1f}">{esc(comp.comment)}</text>')
        add("</g>")

    for p in sheet.powers:
        add(f'<g class="netg" data-net="{esc(p["text"])}">{power_glyph(p, cv)}</g>')

    for lab in sheet.net_labels:
        x, y = cv.pt((lab["x"], lab["y"]))
        add(f'<g class="netg" data-net="{esc(lab["text"])}">'
            f'<text class="t-net {domain(lab["text"])}" x="{x:.1f}" y="{y - 3:.1f}">'
            f'{esc(lab["text"])}</text></g>')

    for p in sheet.ports:
        x, y = cv.pt((p["x"], p["y"]))
        wdt = p["width"] or 50
        add(f'<g class="netg" data-net="{esc(p["name"])}">'
            f'<path class="port" d="M {x:.1f} {y - 6:.1f} h {wdt - 6:.1f} '
            f'l 6 6 l -6 6 h {-(wdt - 6):.1f} Z"/>'
            f'<text class="t-port" x="{x + 6:.1f}" y="{y + 3.5:.1f}">{esc(p["name"])}</text></g>')

    add("</svg>")
    return "\n".join(out)


CSS = """
:root{
  --bg:#f8fafc; --page:#fffdf7; --ink:#10131a; --muted:#5a6a7a; --line:#d3dce6;
  --sym:#1b3a6b; --wire:#123d8c; --sig:#123d8c; --pwr:#b3450c; --gnd:#4a5a6a;
  --local:#8b97a6; --accent:#1b4fd8; --hi:#d92d20; --nc:#d92d20;
  --sans:"IBM Plex Sans",ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0d1219; --page:#131a23; --ink:#dfe8f2; --muted:#8ba0b5; --line:#26313d;
    --sym:#9dc0f0; --wire:#6ea8fe; --sig:#6ea8fe; --pwr:#fb923c; --gnd:#94a3b8;
    --local:#5c6b7d; --accent:#6ea8fe; --hi:#ff7b72; --nc:#ff7b72;
  }
}
:root[data-theme="dark"]{
  --bg:#0d1219; --page:#131a23; --ink:#dfe8f2; --muted:#8ba0b5; --line:#26313d;
  --sym:#9dc0f0; --wire:#6ea8fe; --sig:#6ea8fe; --pwr:#fb923c; --gnd:#94a3b8;
  --local:#5c6b7d; --accent:#6ea8fe; --hi:#ff7b72; --nc:#ff7b72;
}
body{background:var(--bg);color:var(--ink);font:14px/1.55 var(--sans);}
.wrap{max-width:1700px;margin:0 auto;padding:26px 22px 70px;}
h1{font-size:24px;font-weight:600;margin:0 0 4px;}
h2{font-size:16px;font-weight:600;margin:30px 0 10px;padding-bottom:6px;
  border-bottom:1px solid var(--line);}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px;max-width:68ch;}
.sheetbox{background:var(--page);border:1px solid var(--line);border-radius:10px;
  padding:8px;margin:0 0 20px;overflow:auto;max-height:86vh;}
.meta{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;
  margin:0 0 8px;}
.meta b{color:var(--ink);font-weight:600;}
svg.sch{display:block;width:100%;height:auto;min-width:900px;}
.page{fill:var(--page);}
.sym{fill:none;stroke:var(--sym);stroke-width:1.1;}
.fillsym{fill:var(--sym);fill-opacity:.16;}
.wire{fill:none;stroke:var(--wire);stroke-width:1.4;}
.wire.pwr,.pin.pwr{stroke:var(--pwr);} .wire.gnd,.pin.gnd{stroke:var(--gnd);}
.wire.local,.pin.local{stroke:var(--local);}
.pin{stroke:var(--wire);stroke-width:1.4;}
.junction{fill:var(--wire);}
.noerc path{stroke:var(--nc);stroke-width:1.5;fill:none;}
.pwr{stroke:var(--pwr);stroke-width:1.3;fill:none;}
.port{fill:none;stroke:var(--accent);stroke-width:1.1;}
.sheetsym{fill:var(--accent);fill-opacity:.07;stroke:var(--accent);stroke-width:1.2;}
.entry{fill:var(--accent);}
text{font-family:var(--sans);}
.t-des{font-size:8px;font-weight:600;fill:var(--ink);font-family:var(--mono);}
.t-val{font-size:7.5px;fill:var(--muted);font-family:var(--mono);}
.t-pin{font-size:6.5px;fill:var(--muted);font-family:var(--mono);}
.t-num{font-size:6px;fill:var(--muted);font-family:var(--mono);}
.t-net{font-size:7.5px;fill:var(--wire);font-family:var(--mono);}
.t-net.pwr{fill:var(--pwr);} .t-net.gnd{fill:var(--gnd);} .t-net.local{fill:var(--local);}
.t-pwr{font-size:7.5px;fill:var(--pwr);font-family:var(--mono);}
.t-port{font-size:7.5px;fill:var(--accent);font-family:var(--mono);}
.t-sheet{font-size:11px;font-weight:600;fill:var(--accent);}
.t-small{font-size:7px;fill:var(--muted);font-family:var(--mono);}
.netg{cursor:crosshair;}
.netg.hot .wire,.netg.hot .pin{stroke:var(--hi);stroke-width:2.6;}
.netg.hot text{fill:var(--hi);font-weight:700;}
.netg.hot .pwr{stroke:var(--hi);}
.readout{position:sticky;top:0;z-index:5;background:var(--bg);
  padding:6px 0;font-family:var(--mono);font-size:12px;color:var(--muted);}
.readout b{color:var(--hi);}
a{color:var(--accent);}
.toc{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px;}
.toc a{display:inline-block;padding:4px 10px;border:1px solid var(--line);
  border-radius:20px;text-decoration:none;font-size:12px;font-family:var(--mono);}
"""

JS = """
(function(){
  var readout = document.getElementById('readout');
  function setNet(name){
    document.querySelectorAll('.netg.hot').forEach(function(e){e.classList.remove('hot');});
    if(!name){ readout.innerHTML = 'Hover a wire, pin or label to trace its net.'; return; }
    document.querySelectorAll('.netg[data-net="' + CSS.escape(name) + '"]')
      .forEach(function(e){ e.classList.add('hot'); });
    var n = document.querySelectorAll('.netg[data-net="' + CSS.escape(name) + '"]').length;
    readout.innerHTML = 'net <b>' + name + '</b> &middot; ' + n + ' object(s) on this sheet';
  }
  document.addEventListener('mouseover', function(ev){
    var g = ev.target.closest ? ev.target.closest('.netg') : null;
    if(g && g.dataset.net) setNet(g.dataset.net);
  });
  document.addEventListener('mouseleave', function(){ setNet(null); }, true);
  setNet(null);
})();
"""


SVG_STYLE = """<style>
:root{--page:#fffdf7;--ink:#10131a;--muted:#5a6a7a;--sym:#1b3a6b;--wire:#123d8c;
--pwr:#b3450c;--gnd:#4a5a6a;--local:#8b97a6;--accent:#1b4fd8;--nc:#d92d20;
--sans:"IBM Plex Sans",-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",Menlo,Consolas,monospace;}
.page{fill:var(--page);}
.sym{fill:none;stroke:var(--sym);stroke-width:1.1;}
.fillsym{fill:var(--sym);fill-opacity:.16;}
.wire{fill:none;stroke:var(--wire);stroke-width:1.4;}
.wire.pwr,.pin.pwr{stroke:var(--pwr);}.wire.gnd,.pin.gnd{stroke:var(--gnd);}
.wire.local,.pin.local{stroke:var(--local);}
.pin{stroke:var(--wire);stroke-width:1.4;}
.junction{fill:var(--wire);}
.noerc path{stroke:var(--nc);stroke-width:1.5;fill:none;}
.pwr{stroke:var(--pwr);stroke-width:1.3;fill:none;}
.port{fill:none;stroke:var(--accent);stroke-width:1.1;}
.sheetsym{fill:var(--accent);fill-opacity:.07;stroke:var(--accent);stroke-width:1.2;}
.entry{fill:var(--accent);}
text{font-family:var(--sans);}
.t-des{font-size:8px;font-weight:600;fill:var(--ink);font-family:var(--mono);}
.t-val{font-size:7.5px;fill:var(--muted);font-family:var(--mono);}
.t-pin{font-size:6.5px;fill:var(--muted);font-family:var(--mono);}
.t-num{font-size:6px;fill:var(--muted);font-family:var(--mono);}
.t-net{font-size:7.5px;fill:var(--wire);font-family:var(--mono);}
.t-net.pwr{fill:var(--pwr);}.t-net.gnd{fill:var(--gnd);}.t-net.local{fill:var(--local);}
.t-pwr{font-size:7.5px;fill:var(--pwr);font-family:var(--mono);}
.t-port{font-size:7.5px;fill:var(--accent);font-family:var(--mono);}
.t-sheet{font-size:11px;font-weight:600;fill:var(--accent);}
.t-small{font-size:7px;fill:var(--muted);font-family:var(--mono);}
</style>"""


def build(board_keys: list[str]) -> None:
    OUT = P.out / "schematics"
    OUT.mkdir(parents=True, exist_ok=True)
    index: list[str] = []
    name = P.spec["title"] if P.spec else "Altium"
    index.append(f"<title>{esc(name)} Schematics</title>")
    index.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                 'family=IBM+Plex+Mono:wght@400;500;600&'
                 'family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">')
    index.append(f"<style>{CSS}</style>")
    index.append('<div class="wrap">')
    index.append(f"<h1>{esc(name)} &mdash; schematic sheets</h1>")
    index.append('<p class="sub">Drawn directly from the .SchDoc files with the '
                 'design\'s own symbols, and coloured by the offline netlist. '
                 'Hover any wire, pin or label to trace a net across the sheet.</p>')
    index.append('<div class="readout" id="readout"></div>')

    toc: list[str] = []
    bodies: list[str] = []

    for key in board_keys:
        cfg = BOARDS[key]
        result = build_board(sheet_paths(key), flat=cfg["flat"])
        node_net: dict = {}
        for net in result["nets"]:
            for nd in net["nodes"]:
                base = nd["designator"]
                if net["room"] and base.endswith("_" + net["room"]):
                    base = base[: -(len(net["room"]) + 1)]
                node_net[(base, nd["pin"])] = net["name"]

        sheets = result["sheets"]
        bodies.append(f'<h2 id="{key}">{esc(cfg["title"])}</h2>')
        for name, sheet in sheets.items():
            # a wire carries the net of any pin end sitting on it
            xy_net = {}
            for comp in sheet.components:
                for pin in comp.pins:
                    n = node_net.get((comp.designator, pin.designator))
                    if n:
                        xy_net[("xy", pin.end)] = n
            merged = dict(node_net)
            merged.update(xy_net)

            svg = render_sheet(sheet, merged, f"{cfg['title']} / {name}")
            safe = name.replace(".SchDoc", "")
            path = OUT / key / f"{safe}.svg"
            path.parent.mkdir(parents=True, exist_ok=True)
            # A standalone .svg carries no page stylesheet, so the drawing
            # rules have to travel inside it or every shape falls back to a
            # black fill and the sheet renders as a solid rectangle.
            head, sep, tail = svg.partition(">")
            standalone = head + sep + "\n" + SVG_STYLE + tail
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + standalone,
                            encoding="utf-8")
            anchor = f"{key}-{safe}"
            toc.append(f'<a href="#{anchor}">{esc(safe)}</a>')
            bodies.append(f'<h3 id="{anchor}" style="font-size:13px;margin:18px 0 6px">'
                          f'{esc(safe)}</h3>')
            bodies.append('<div class="meta">'
                          f'<span><b>{len(sheet.components)}</b> components</span>'
                          f'<span><b>{len(sheet.wires)}</b> wires</span>'
                          f'<span><b>{len(sheet.net_labels)}</b> net labels</span>'
                          f'<span><b>{len(sheet.powers)}</b> power ports</span>'
                          f'<span><b>{len(sheet.ports)}</b> ports</span>'
                          f'<span><a href="{key}/{esc(safe)}.svg">standalone svg</a></span>'
                          '</div>')
            bodies.append(f'<div class="sheetbox">{svg}</div>')
            print(f"  {key}/{safe}.svg")

    index.append('<div class="toc">' + "".join(toc) + "</div>")
    index.extend(bodies)
    index.append("</div>")
    index.append(f"<script>{JS}</script>")
    (OUT / "index.html").write_text("\n".join(index), encoding="utf-8")
    print(f"\nwrote {OUT / 'index.html'}")


if __name__ == "__main__":
    args, ref = project.take_project_arg(sys.argv[1:])
    project.use(ref)
    build(args or list(BOARDS))
