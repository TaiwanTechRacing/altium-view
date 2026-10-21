#!/usr/bin/env python3
"""
Routed length the way a length-tuning engineer measures it: pad to pad along
the copper actually drawn, not the sum of every track on the net.

A net's copper is turned into a graph. Track and arc ends are nodes; a track
whose end lands on the middle of another splits it (a T); a via is one node
per copper layer it spans, joined by its barrel; a pad is a node at its centre,
reached from every track end that lies on it. The shortest path between two
pads is then the routed length of that connection, so stubs, test-point
branches and overlapping duplicate segments no longer inflate it the way a
plain sum of track lengths does.

Two numbers are reported, because tools differ on vias:
  planar  -- tracks and arcs only (Altium's routed length with via length off)
  with z  -- planar plus the via barrel between the layers actually used,
             from the stack-up (Altium with "include via length" on)

An xSignal runs through series parts (a 0R or 33R in an SDRAM line): its
length is the sum of the connections on each side. The part's own
pad-to-pad pitch is reported separately and not added, as Altium does.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import defaultdict

SNAP = 0.005          # mm: ends closer than this are the same point
ON_SEG = 0.01         # mm: an end this close to a segment's middle makes a T


def layer_z(pcb: dict) -> dict[int, float]:
    """Height of the middle of each copper layer, mm from the top surface."""
    z, out = 0.0, {}
    for s in pcb["stack"]:
        if s.get("kind") == "copper":
            t = s.get("copper_mm") or 0.0
            out[s["layer"]] = z + t / 2
            z += t
        elif s.get("kind") == "dielectric":
            z += s.get("height_mm") or 0.0
    return out


def _arc_ends(a: dict):
    sa, ea = math.radians(a["start"]), math.radians(a["end"])
    return ((a["cx"] + a["r"] * math.cos(sa), a["cy"] + a["r"] * math.sin(sa)),
            (a["cx"] + a["r"] * math.cos(ea), a["cy"] + a["r"] * math.sin(ea)))


def _arc_len(a: dict) -> float:
    sweep = (a["end"] - a["start"]) % 360 or 360
    return math.radians(sweep) * a["r"]


def _proj(p, a, b):
    """Parameter and distance of p projected on segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return 0.0, math.dist(p, a)
    u = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2
    u = max(0.0, min(1.0, u))
    return u, math.dist(p, (a[0] + u * dx, a[1] + u * dy))


def _in_pad(pt, pad, margin: float = 0.0) -> bool:
    """Is pt on the pad, the pad grown by `margin` (half the track width:
    a track whose centreline stops just short of the pad edge still lands
    its copper on it)?"""
    dx, dy = pt[0] - pad["x"], pt[1] - pad["y"]
    if pad["shape"] == "round" and abs(pad["sx"] - pad["sy"]) < 1e-3:
        return math.hypot(dx, dy) <= pad["sx"] / 2 + margin + SNAP
    a = math.radians(pad["rotation"])
    lx = dx * math.cos(a) + dy * math.sin(a)
    ly = -dx * math.sin(a) + dy * math.cos(a)
    return (abs(lx) <= pad["sx"] / 2 + margin + SNAP
            and abs(ly) <= pad["sy"] / 2 + margin + SNAP)


class NetGraph:
    """Copper of one net as a weighted graph; see the module docstring."""

    def __init__(self, pcb: dict, net: str, pad_corners, copper: list[int]):
        self.pcb, self.net = pcb, net
        self.copper = copper
        self.order = {l: i for i, l in enumerate(copper)}
        self.z = layer_z(pcb)
        self.adj: dict = defaultdict(list)          # node -> [(node, planar, z, via_id)]
        self.pad_nodes: dict = {}                   # (des, pad) -> [node]
        self._build(pad_corners)

    # nodes are (layer, x, y) on a SNAP grid
    @staticmethod
    def _key(layer, x, y):
        return (layer, round(x / SNAP), round(y / SNAP))

    def _edge(self, a, b, planar=0.0, z=0.0, via=None):
        if a == b:
            return
        self.adj[a].append((b, planar, z, via))
        self.adj[b].append((a, planar, z, via))

    def _build(self, pad_corners):
        pcb, net, order = self.pcb, self.net, self.order
        segs = []          # (layer, p0, p1, kind, length-or-None, half width)
        for t in pcb["tracks"]:
            if t["net"] == net and t["layer"] in order and not t.get("keepout"):
                segs.append((t["layer"], (t["x1"], t["y1"]), (t["x2"], t["y2"]), "t", None, t["width"] / 2))
        for a in pcb["arcs"]:
            if a["net"] == net and a["layer"] in order and not a.get("keepout"):
                p0, p1 = _arc_ends(a)
                segs.append((a["layer"], p0, p1, "a", _arc_len(a), a["width"] / 2))
        vias = [v for v in pcb["vias"] if v["net"] == net]
        pads = [p for p in pcb["pads"] if p["net"] == net
                and (p["layer"] in order or p["layer"] == 74)]

        # every point that can split a straight track: ends of other
        # segments, via centres, pad centres
        pts_by_layer = defaultdict(list)
        for l, p0, p1, _, _, _ in segs:
            pts_by_layer[l] += [p0, p1]
        for v in vias:
            for l in self._via_layers(v):
                pts_by_layer[l].append((v["x"], v["y"]))
        for p in pads:
            for l in self._pad_layers(p):
                pts_by_layer[l].append((p["x"], p["y"]))

        for l, p0, p1, kind, alen, hw in segs:
            if kind == "a":
                self._edge(self._key(l, *p0), self._key(l, *p1), planar=alen)
                continue
            cuts = [(0.0, p0), (1.0, p1)]
            for q in pts_by_layer[l]:
                u, d = _proj(q, p0, p1)
                if d <= max(ON_SEG, hw) and 1e-6 < u < 1 - 1e-6:
                    cuts.append((u, (p0[0] + u * (p1[0] - p0[0]), p0[1] + u * (p1[1] - p0[1]))))
            cuts.sort()
            for (u0, a), (u1, b) in zip(cuts, cuts[1:]):
                self._edge(self._key(l, *a), self._key(l, *b), planar=math.dist(a, b))

        ends_by_layer = defaultdict(dict)     # layer -> {end point: half width}
        for l, p0, p1, _, _, hw in segs:
            for e in (p0, p1):
                ends_by_layer[l][e] = max(hw, ends_by_layer[l].get(e, 0.0))

        # vias: barrel between consecutive spanned layers; track ends that sit
        # on the via land join its node on that layer
        for vi, v in enumerate(vias):
            ls = self._via_layers(v)
            for la, lb in zip(ls, ls[1:]):
                self._edge(self._key(la, v["x"], v["y"]), self._key(lb, v["x"], v["y"]),
                           z=abs(self.z.get(la, 0) - self.z.get(lb, 0)), via=vi)
            for l in ls:
                c = (v["x"], v["y"])
                for e, hw in ends_by_layer[l].items():
                    # entering the via land is not track length (Altium
                    # counts track only), but it is a connection
                    if math.dist(e, c) <= v["diameter"] / 2 + hw + SNAP:
                        self._edge(self._key(l, *e), self._key(l, *c))

        # pads: centre node per layer, joined to every track end on the pad
        for p in pads:
            ls = self._pad_layers(p)
            nodes = []
            for l in ls:
                c = (p["x"], p["y"])
                n = self._key(l, *c)
                nodes.append(n)
                self.adj.setdefault(n, [])
                for e, hw in ends_by_layer[l].items():
                    if _in_pad(e, p, hw):
                        self._edge(self._key(l, *e), n)      # pad entry: not track length
            for la, lb in zip(ls, ls[1:]):          # through-hole barrel
                self._edge(self._key(la, p["x"], p["y"]), self._key(lb, p["x"], p["y"]),
                           z=abs(self.z.get(la, 0) - self.z.get(lb, 0)), via=f"pad:{p['component']}.{p['name']}")
            self.pad_nodes[(p["component"], p["name"])] = nodes

    def _via_layers(self, v):
        ia = self.order.get(v["from"], 0)
        ib = self.order.get(v["to"], len(self.copper) - 1)
        lo, hi = min(ia, ib), max(ia, ib)
        return self.copper[lo:hi + 1]

    def _pad_layers(self, p):
        return list(self.copper) if p["layer"] == 74 else [p["layer"]]

    def path(self, a: tuple[str, str], b: tuple[str, str]):
        """Shortest copper path pad a -> pad b, weighted by planar + z length."""
        src = self.pad_nodes.get(a)
        dst = set(self.pad_nodes.get(b) or [])
        if not src or not dst:
            return None
        dist = {n: 0.0 for n in src}
        info = {n: (0.0, 0.0, frozenset()) for n in src}   # planar, z, vias
        pq = [(0.0, i, n) for i, n in enumerate(src)]
        heapq.heapify(pq)
        tick = len(pq)
        while pq:
            d, _, n = heapq.heappop(pq)
            if d > dist.get(n, math.inf):
                continue
            if n in dst:
                pl, zz, vs = info[n]
                return {"planar": pl, "z": zz, "total": pl + zz,
                        "vias": len([v for v in vs if not str(v).startswith("pad:")]),
                        "layers": sorted({k[0] for k in self._trace(n, src)})}
            for m, pl, zz, via in self.adj[n]:
                nd = d + pl + zz
                if nd < dist.get(m, math.inf) - 1e-9:
                    dist[m] = nd
                    p0, z0, v0 = info[n]
                    info[m] = (p0 + pl, z0 + zz, v0 | ({via} if via is not None else frozenset()))
                    self._prev[m] = n
                    tick += 1
                    heapq.heappush(pq, (nd, tick, m))
        return None

    _prev: dict

    def _trace(self, n, src):
        out = [n]
        while n not in src and n in self._prev:
            n = self._prev[n]
            out.append(n)
        return out


def _graph(pcb, net, pad_corners, copper, cache):
    g = cache.get(net)
    if g is None:
        g = NetGraph(pcb, net, pad_corners, copper)
        g._prev = {}
        cache[net] = g
    else:
        g._prev = {}
    return g


def connection(pcb, a: dict, b: dict, pad_corners, copper, cache=None):
    """Routed length pad a -> pad b on one net (dict or None when not joined)."""
    cache = {} if cache is None else cache
    if a["net"] != b["net"] or not a["net"]:
        return None
    g = _graph(pcb, a["net"], pad_corners, copper, cache)
    return g.path((a["component"], a["name"]), (b["component"], b["name"]))


def xsignals(pcb, src: str, dst: str, pad_corners, copper, max_pads: int = 4):
    """
    Every connection from part `src` to part `dst`: direct on one net, or
    through one two-pad part (a series resistor). Returns a list of rows.
    """
    pads = defaultdict(list)
    for p in pcb["pads"]:
        pads[p["component"]].append(p)
    src_by_net = defaultdict(list)
    for p in pads.get(src, []):
        if p["net"]:
            src_by_net[p["net"]].append(p)
    series = defaultdict(list)            # net -> [(part, pad on net, other pad)]
    for des, ps in pads.items():
        if des in (src, dst) or len(ps) != 2 or not all(q["net"] for q in ps):
            continue
        a, b = ps
        if a["net"] == b["net"]:
            continue
        series[a["net"]].append((des, a, b))
        series[b["net"]].append((des, b, a))

    # a supply net touches dozens of pads; a signal touches a handful (two
    # ends, maybe a pull-up). Supplies are not xSignals.
    pads_on = defaultdict(int)
    for p in pcb["pads"]:
        if p["net"]:
            pads_on[p["net"]] += 1

    cache, rows = {}, []
    for dp in pads.get(dst, []):
        n = dp["net"]
        if not n or pads_on[n] > max_pads:
            continue
        if n in src_by_net:
            for sp in src_by_net[n]:
                r = connection(pcb, sp, dp, pad_corners, copper, cache)
                rows.append({"src": sp, "dst": dp, "nets": [n], "via_part": None, "legs": [r]})
            continue
        for des, near, far in series.get(n, []):
            if pads_on[far["net"]] > max_pads:
                continue                      # a pull-up to a rail, not the signal's path
            for sp in src_by_net.get(far["net"], []):
                r1 = connection(pcb, sp, far, pad_corners, copper, cache)
                r2 = connection(pcb, near, dp, pad_corners, copper, cache)
                rows.append({"src": sp, "dst": dp, "nets": [far["net"], n],
                             "via_part": des, "pitch": math.dist((near["x"], near["y"]), (far["x"], far["y"])),
                             "legs": [r1, r2]})
    for r in rows:
        legs = r["legs"]
        r["ok"] = all(legs)
        r["planar"] = sum(l["planar"] for l in legs if l)
        r["z"] = sum(l["z"] for l in legs if l)
        r["total"] = r["planar"] + r["z"]
        r["vias"] = sum(l["vias"] for l in legs if l)
        r["name"] = r["nets"][0]
        # the leg after the series part only (MCU -> R is often left out of
        # length matching when the resistor sits at the driver)
        last = legs[-1]
        r["after_planar"] = last["planar"] if last else 0.0
        r["after_total"] = last["total"] if last else 0.0
        r["before_planar"] = legs[0]["planar"] if len(legs) > 1 and legs[0] else 0.0
    return rows


def glob_match(name: str, patterns: list[str]) -> bool:
    return any(re.fullmatch(p.replace("*", ".*").replace("?", "."), name) for p in patterns)
