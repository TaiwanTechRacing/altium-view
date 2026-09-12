#!/usr/bin/env python3
"""
Diff the offline netlist against the netlist Altium exported, node by node.

The offline reader is only worth using if it reproduces Altium exactly, so
this is the gate: `raw/<Board>.net` is the golden file, and any difference is
printed rather than tolerated.

Run:  python validate_offline.py [Board ...]
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import project
from boards import BOARDS, sheet_paths
from project import P
from netlist_offline import build_board

ROOT = Path(__file__).resolve().parent


def norm(designator: str, pin: str) -> str:
    return f"{designator}.{re.sub(r'\\s+', '', str(pin))}"


def golden(path: Path, known: set[str]) -> dict[str, set[str]]:
    """Parse a PADS netlist, cutting nodes at known designators (pin
    designators may themselves contain spaces)."""
    nets: dict[str, set[str]] = {}
    cur = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("*SIGNAL*"):
            cur = line.split(None, 1)[1].strip()
            nets[cur] = set()
            continue
        if line.startswith("*"):
            cur = None
            continue
        if cur is None:
            continue
        des, pin = None, []
        for tok in line.split():
            head = tok.split(".", 1)
            if len(head) == 2 and head[0] in known:
                if des is not None:
                    nets[cur].add(norm(des, " ".join(pin)))
                des, pin = head[0], [head[1]]
            elif des is not None:
                pin.append(tok)
        if des is not None:
            nets[cur].add(norm(des, " ".join(pin)))
    return nets


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def stale_reference(board_key: str, net_file: Path) -> tuple[str, str] | None:
    """
    Is the golden netlist older than the schematic it is supposed to check?

    A reference exported before the last edit disagrees with the design by
    construction, and reading that as "the netlister broke" sends the reader
    hunting a bug that is not there. Rather than hide the difference, say which
    side is out of date.
    """
    newest = max(sheet_paths(board_key), key=lambda p: p.stat().st_mtime)
    if newest.stat().st_mtime <= net_file.stat().st_mtime:
        return None
    return (when(net_file.stat().st_mtime),
            f"{newest.name} {when(newest.stat().st_mtime)}")


def check(board_key: str) -> bool:
    cfg = BOARDS[board_key]
    net_file = P.raw / f"{board_key}.net"
    if not net_file.exists():
        print(f"{board_key}: no golden netlist at {net_file.name}, skipping")
        return None
    stale = stale_reference(board_key, net_file)

    result = build_board(sheet_paths(board_key), flat=cfg["flat"])
    mine: dict[str, set[str]] = {}
    for n in result["nets"]:
        mine.setdefault(n["name"], set()).update(
            norm(nd["designator"], nd["pin"]) for nd in n["nodes"])
    mine = {k: v for k, v in mine.items() if v}

    known = {d.split(".")[0] for s in mine.values() for d in s}
    gold = golden(net_file, known)
    gold = {k: v for k, v in gold.items() if v}

    # compare as node -> net-name maps, and as raw partitions
    g_node = {n: name for name, nodes in gold.items() for n in nodes}
    m_node = {n: name for name, nodes in mine.items() for n in nodes}

    # A designator used twice makes 'R?.1' name two different physical pins, so
    # neither netlist can be keyed by it. Those nodes are reported separately
    # rather than counted as a mismatch the reader cannot act on.
    ambiguous = {d for d in known if "?" in d}
    def amb(node: str) -> bool:
        return node.split(".")[0] in ambiguous

    missing = sorted(n for n in set(g_node) - set(m_node) if not amb(n))
    extra = sorted(n for n in set(m_node) - set(g_node) if not amb(n))
    renamed = sorted(n for n in set(g_node) & set(m_node)
                     if g_node[n] != m_node[n] and not amb(n))
    amb_nodes = sorted(n for n in set(g_node) | set(m_node) if amb(n))

    # partition equality ignores naming: are the same pins grouped together?
    strip = lambda s: frozenset(x for x in s if not amb(x))
    g_part = {strip(v) for v in gold.values()} - {frozenset()}
    m_part = {strip(v) for v in mine.values()} - {frozenset()}
    part_ok = g_part == m_part

    ok = not missing and not extra and not renamed
    print(f"\n=== {board_key} ===")
    if stale:
        exported, edited = stale
        print(f"  STALE REFERENCE -- netlist exported {exported}, but "
              f"{edited} is newer.")
        print("  Differences below are probably edits made since, not "
              "errors. Re-export from Altium.")
    print(f"  golden {len(gold):>4} nets / {len(g_node):>4} nodes")
    print(f"  offline{len(mine):>4} nets / {len(m_node):>4} nodes")
    print(f"  grouping identical: {part_ok}")
    if amb_nodes:
        print(f"  {len(amb_nodes)} node(s) on un-annotated designators, not comparable: "
              f"{', '.join(amb_nodes)}")
    if result["orphans"]:
        print(f"  {len(result['orphans'])} unattached label/power/port:")
        for o in result["orphans"][:8]:
            print(f"      {o}")
    if missing:
        print(f"  MISSING {len(missing)} node(s) the golden netlist has:")
        for n in missing[:12]:
            print(f"      {n:<22} golden net {g_node[n]}")
    if extra:
        print(f"  EXTRA {len(extra)} node(s) not in the golden netlist:")
        for n in extra[:12]:
            print(f"      {n:<22} offline net {m_node[n]}")
    if renamed:
        print(f"  RENAMED {len(renamed)} node(s):")
        shown = {}
        for n in renamed:
            shown.setdefault((g_node[n], m_node[n]), []).append(n)
        for (g, m), ns in list(shown.items())[:14]:
            print(f"      golden {g:<26} offline {m:<26} ({len(ns)}) e.g. {ns[0]}")
    if ok:
        print("  MATCH")
    STALE[board_key] = bool(stale)
    return ok


STALE: dict[str, bool] = {}


def main(argv: list[str]) -> int:
    argv, ref = project.take_project_arg(argv)
    project.use(ref)
    keys = argv or list(BOARDS)
    results = [check(k) for k in keys]
    checked = [r for r in results if r is not None]
    print()
    if not checked:
        # saying "all match" after checking nothing is the kind of green tick
        # that hides a broken pipeline
        print(f"NOTHING CHECKED -- no Altium-exported netlist in {P.raw}.\n"
              "Export one per board from Altium (see EXTRACT.md) to keep the "
              "offline netlister under test.")
        return 2
    if len(checked) < len(results):
        print(f"note: {len(results) - len(checked)} board(s) had no golden netlist")
    if all(checked):
        print("all checked boards match")
        return 0
    if all(STALE.get(k) for k, r in zip(keys, results)
           if r is False):
        print("MISMATCHES, but every board that differs is compared against a"
              " netlist older than its schematic.")
        print("Re-export the golden netlists (see EXTRACT.md) before reading"
              " this as a netlister fault.")
        return 3
    print("MISMATCHES -- see above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
