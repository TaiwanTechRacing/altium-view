#!/usr/bin/env python3
"""
Answer questions about an Altium design from its extracted system model.

The point is to avoid loading an 800 KB model to learn one fact. Every command
prints a few lines; nothing here dumps the whole design.

  python query.py summary                     the system in ~30 lines
  python query.py part IC20                   one component, every pin and net
  python query.py net FLASH_CS                every node on a net, all boards
  python query.py part Control/C1             the C1 on one named board
  python query.py block ISOCAN                a functional sheet
  python query.py link C_9                    one board-to-board connector pair
  python query.py find ISO1042                search parts, nets, MPNs
  python query.py trace IC20.51               follow a signal across boards
  python query.py nets IC20                   every net a component touches
  python query.py netlist                     every connection, compact
  python query.py freshness                   is the model still current?
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import model_io
import project                  # noqa: E402
from project import P           # noqa: E402


CAVEATS: list[str] = []


def load(auto_refresh: bool = True) -> dict:
    """
    The model is a cache of the Altium files, so verify it before trusting it.

    Checking costs a few milliseconds; rebuilding costs about a second. That is
    cheap enough to do on every read, which is what keeps a shared snapshot
    from quietly serving yesterday's design to a session that has no idea the
    schematic moved.

    Anything that weakens the answer is collected in CAVEATS and printed on
    stdout above it. A warning on stderr is not enough: the reader here is
    usually an agent capturing stdout, and an answer that looks confident while
    its caveat went somewhere else is exactly how a stale or unchecked model
    gets quoted as fact.
    """
    if auto_refresh:
        try:
            from freshness import ensure_fresh
            st = ensure_fresh()
            if st.get("rebuilt"):
                print("[altium-view] model rebuilt from the current design files",
                      file=sys.stderr)
            elif not st["fresh"]:
                CAVEATS.append(
                    f"STALE: the model does not match the design files "
                    f"({st['reason']}) and could not be rebuilt. Answers below "
                    f"describe an older version of the schematic.")
        except Exception as exc:                       # noqa: BLE001
            CAVEATS.append(f"UNCHECKED: the freshness check failed ({exc}), so "
                           f"this answer may predate the current schematic.")
    if not (P.out / "system_model.json").exists():
        sys.exit("system_model.json missing -- run: python extract_offline.py "
                 "&& python build_system_model.py")
    model = model_io.read(P.out / "system_model.json")
    CAVEATS.extend(verification_caveats(model))
    global _short
    from naming import shortener
    _short = shortener([blk["sheet"] for b in model["boards"] for blk in b["blocks"]])
    return model


def verification_caveats(model: dict) -> list[str]:
    """
    What the model itself says about how far it has been checked.

    The netlist is only known-good where Altium's own export was available and
    agreed with it node for node. Every other state -- no export, an export
    older than the schematic, an export that disagrees -- means the
    connectivity below is derived and unchecked, and saying so costs one line.
    """
    no_ref, stale, differs = [], [], []
    for b in model["boards"]:
        cc = b.get("cross_check") or {}
        if cc.get("stale_golden_exported_utc"):
            stale.append(b["title"])
        elif not str(cc.get("reference", "")).startswith("altium"):
            no_ref.append(b["title"])
        elif cc.get("only_in_netlist") or cc.get("only_in_model"):
            differs.append(b["title"])
    out = []
    if differs:
        out.append(f"DISAGREES with Altium's own netlist on: {', '.join(differs)}. "
                   f"Run: altium_view.py validate")
    if stale:
        out.append(f"UNVERIFIED: {', '.join(stale)} -- Altium's exported netlist "
                   f"predates the schematic, so nothing independent checked this "
                   f"connectivity. Re-export it (EXTRACT.md).")
    if no_ref:
        out.append(f"UNVERIFIED: {', '.join(no_ref)} -- no Altium-exported netlist "
                   f"to check against. The connectivity below is derived by this "
                   f"tool and has not been confirmed.")
    return out


def print_caveats() -> None:
    if not CAVEATS:
        return
    for line in CAVEATS:
        print(f"!! {line}")
    print()


_short = None


def short(sheet: str) -> str:
    if _short is None:
        return (sheet or "").replace(".SchDoc", "")
    return _short(sheet)


def nc(node: dict) -> str:
    """Marker for a pin the designer marked as deliberately open (No ERC)."""
    return "  NC" if node.get("no_erc") else ""


def boards_for(m: dict, args: list, has) -> tuple[list[dict], list[dict]]:
    """
    Every board the thing is on, and the subset asked for.

    A designator is unique inside one board, not across a multi-board design:
    66 designators in the design this was built against exist on more
    than one board. Answering
    from whichever board happened to come first, with no sign that the others
    exist, is how a question about C1 gets answered about a different C1.
    """
    hits = [b for b in m["boards"] if has(b)]
    if len(args) > 1 and hits:
        want = args[1].lower()
        narrowed = [b for b in hits
                    if want in b["title"].lower() or want in b["key"].lower()]
        if narrowed:
            return narrowed, hits
        print(f"!! no board matching {args[1]!r} has it -- showing all")
    return hits, hits


def scope_note(want: str, shown: list[dict], hits: list[dict]) -> None:
    """Say when an answer is one of several, or one of several was chosen."""
    if len(hits) < 2:
        return
    if len(shown) == len(hits):
        print(f"!! {want} exists on {len(hits)} boards: "
              f"{', '.join(b['title'] for b in hits)} -- all shown; add a board "
              f"name to narrow, e.g. {hits[0]['title'].split()[0]}")
    else:
        others = [b["title"] for b in hits if b not in shown]
        print(f"!! showing {', '.join(b['title'] for b in shown)}; {want} also "
              f"exists on {', '.join(others)}")
    print()


def board_of(m: dict, key: str) -> dict:
    return next(b for b in m["boards"] if b["key"] == key)


# --------------------------------------------------------------------------

def cmd_summary(m: dict, _args) -> None:
    print(f"{m['system']['name']}   generated {m['generated_utc']}")
    print()
    for b in m["boards"]:
        print(f"{b['title']}  ({b['component_count']} parts, {b['net_count']} nets, "
              f"{b['sheet_count']} sheet(s))")
        for blk in b["blocks"]:
            rooms = f" x{len(blk['rooms'])}" if blk["rooms"] else ""
            parts = ", ".join(f"{k['designator']} {k['comment']}"
                              for k in blk["key_parts"][:4]) or "passives only"
            print(f"   {short(blk['sheet']):<16}{rooms:<4} {parts}")
    print()
    print("board-to-board links")
    for lk in m["interboard_links"]:
        print(f"   {lk['designator']:<5} {lk['a_board'][:13]:<13} {lk['a_connector']:<16}"
              f" -> {lk['b_board'][:13]:<13} {lk['b_connector']:<16} {lk['wire_count']}w")
    issues = [(b["title"], i) for b in m["boards"] for i in b["issues"]]
    print()
    print(f"{len(issues)} item(s) flagged; run: altium_view.py issues")


def cmd_issues(m: dict, _args) -> None:
    for b in m["boards"]:
        for i in b["issues"]:
            print(f"[{i['severity']}] {b['title']} / {i['kind']}: {i['where']}")
            print(f"    {i['detail']}")


def cmd_part(m: dict, args) -> None:
    want = args[0]
    shown, hits = boards_for(m, args, lambda b: want in b["components"])
    if not hits:
        print(f"no component {want!r}; try: python query.py find {want}")
        return
    scope_note(want, shown, hits)
    for b in shown:
        c = b["components"][want]
        if c.get("ambiguous"):
            print(f"!! {want} is used by {c['ambiguous']} different parts on "
                  f"{b['title']}. Only one of them is described below, and "
                  f"nothing keyed by designator can tell them apart.")
        print(f"{c['designator']}   {c['comment']}   [{c['lib_ref']}]")
        print(f"  board   {b['title']}")
        print(f"  sheet   {short(c['sheet'])}" + (f"   channel {c['room']}" if c["room"] else ""))
        print(f"  package {c['footprint']}")
        if c.get("mpn"):
            print(f"  mpn     {c['mpn']}  ({c.get('manufacturer') or '?'})")
        if c.get("datasheet"):
            print(f"  datasheet {c['datasheet']}")
        print(f"  kind    {c['kind']}   {c['pin_count']} pins")
        print("  pins:")
        pins = defaultdict(list)
        for net_name, rec in b["nets"].items():
            for nd in rec["nodes"]:
                if nd["designator"] == want:
                    pins[nd["pin"]].append((nd.get("pin_name"), net_name, nc(nd)))
        for pin in sorted(pins, key=lambda s: (len(s), s)):
            for pin_name, net, mark in pins[pin]:
                print(f"     {pin:<8} {str(pin_name or ''):<18} {net}{mark}")
        if c.get("no_connect_pins"):
            print(f"  NC = marked No ERC in the schematic, deliberately open: "
                  f"{', '.join(c['no_connect_pins'])}")
        if c.get("sections", 1) > 1:
            print(f"  one physical part placed as {c['sections']} sections "
                  f"({', '.join(c.get('sheets') or [])})")
        print()


def cmd_net(m: dict, args) -> None:
    want = args[0]
    shown, hits = boards_for(m, args, lambda b: want in b["nets"])
    scope_note(want, shown, hits)
    found = bool(hits)
    for b in shown:
        rec = b["nets"][want]
        print(f"{want}   [{b['title']}]  {rec['domain']}, {rec['node_count']} pins, "
              f"{len(rec['sheets'])} sheet(s)")
        for nd in sorted(rec["nodes"], key=lambda n: (n["designator"], n["pin"])):
            comp = b["components"].get(nd["designator"], {})
            print(f"   {nd['designator']:<16}.{nd['pin']:<6} {str(nd.get('pin_name') or ''):<16}"
                  f" {comp.get('comment', ''):<24} {short(nd['sheet'])}{nc(nd)}")
    for lk in m["interboard_links"]:
        for w in lk["wires"]:
            if want in (w["a_net"], w["b_net"]):
                print(f"   crosses {lk['designator']}: {lk['a_board']} "
                      f"{lk['a_connector']}.{w['a_pin']} ({w['a_net']}) <-> "
                      f"{lk['b_board']} {lk['b_connector']}.{w['b_pin']} ({w['b_net']})")
                found = True
    if not found:
        print(f"no net {want!r}; try: python query.py find {want}")


def cmd_nets(m: dict, args) -> None:
    want = args[0]
    shown, hits = boards_for(m, args, lambda b: want in b["components"])
    if not hits:
        print(f"no component {want!r}")
        return
    scope_note(want, shown, hits)
    for b in shown:
        rows = []
        for net_name, rec in b["nets"].items():
            for nd in rec["nodes"]:
                if nd["designator"] == want:
                    rows.append((nd["pin"], nd.get("pin_name"), net_name,
                                 rec["domain"], nc(nd)))
        print(f"{want} [{b['title']}]  {len(rows)} pins")
        for pin, pname, net, dom, mark in sorted(rows, key=lambda r: (len(r[0]), r[0])):
            print(f"   {pin:<8} {str(pname or ''):<18} {net:<26} {dom}{mark}")
        print()


def cmd_block(m: dict, args) -> None:
    want = args[0].upper()
    matches = [(b, blk) for b in m["boards"] for blk in b["blocks"]
               if want in short(blk["sheet"]).upper()]
    if not matches:
        print(f"no block matching {want!r}")
        return
    if len(matches) > 1:
        # 'POWER' matches a power sheet on three different boards here; showing
        # the first and staying silent about the rest answers a question the
        # reader did not ask
        print(f"!! {len(matches)} sheets match {want!r}: "
              + ", ".join(f"{short(blk['sheet'])} [{b['title']}]"
                          for b, blk in matches))
        print()
    for b, blk in matches[:4]:
        print(f"{short(blk['sheet'])}   [{b['title']}]")
        if blk["rooms"]:
            print(f"  repeated x{len(blk['rooms'])}: {', '.join(blk['rooms'])}")
        print(f"  {blk['component_count']} parts")
        if blk["connectors"]:
            print(f"  connectors: {', '.join(blk['connectors'])}")
        print("  key devices:")
        for k in blk["key_parts"]:
            print(f"     {k['designator']:<12} {k['comment']:<28} {k.get('mpn') or ''}")
        ins = [bl for bl in b["block_links"] if blk["sheet"] in (bl["a"], bl["b"])]
        if ins:
            print("  signals to other sheets:")
            for bl in ins:
                other = bl["b"] if bl["a"] == blk["sheet"] else bl["a"]
                print(f"     {short(other):<16} {bl['net_count']:>3}: "
                      f"{', '.join(bl['nets'][:10])}")
        print()
    if len(matches) > 4:
        print(f"({len(matches) - 4} more match; be more specific)")


def cmd_link(m: dict, args) -> None:
    want = args[0].upper()
    for lk in m["interboard_links"]:
        if want not in lk["designator"].upper() and \
           want not in (lk["a_connector"] + lk["b_connector"]).upper():
            continue
        print(f"{lk['designator']}   {lk['a_board']} {lk['a_connector']} <-> "
              f"{lk['b_board']} {lk['b_connector']}   {lk['wire_count']} wires")
        print(f"   {short(lk['a_block'] or '')} <-> {short(lk['b_block'] or '')}")
        for w in lk["wires"]:
            print(f"     pin {str(w['pin']):<4} {str(w['a_pin']):<4} {str(w['a_net'] or ''):<24}"
                  f" <-> {str(w['b_pin']):<4} {w['b_net'] or ''}")
        return
    print(f"no link matching {want!r}")


def cmd_find(m: dict, args) -> None:
    pat = re.compile(re.escape(args[0]), re.IGNORECASE)
    hits = 0
    for b in m["boards"]:
        for des, c in b["components"].items():
            blob = " ".join(str(c.get(k) or "") for k in
                            ("designator", "comment", "lib_ref", "footprint", "mpn"))
            if pat.search(blob):
                print(f"part  {des:<16} {c['comment']:<26} {short(c['sheet']):<16} {b['title']}")
                hits += 1
        for name, rec in b["nets"].items():
            if pat.search(name):
                print(f"net   {name:<26} {rec['node_count']:>3} pins  "
                      f"{rec['domain']:<7} {b['title']}")
                hits += 1
    if not hits:
        print(f"nothing matches {args[0]!r}")


def cross_board_index(m: dict):
    """Nets on either side of every inter-board wire, so a trace can hop boards."""
    link: dict[tuple[str, str], list] = defaultdict(list)
    title_key = {b["title"]: b["key"] for b in m["boards"]}
    for lk in m["interboard_links"]:
        ak, bk = title_key.get(lk["a_board"]), title_key.get(lk["b_board"])
        for w in lk["wires"]:
            if not (w["a_net"] and w["b_net"]):
                continue
            link[(ak, w["a_net"])].append((bk, w["b_net"], lk["designator"]))
            link[(bk, w["b_net"])].append((ak, w["a_net"], lk["designator"]))
    return link


def cmd_trace(m: dict, args) -> None:
    """Follow a pin's net, then hop across every board-to-board link it reaches."""
    target = args[0]
    if "." not in target:
        sys.exit("give a node as DESIGNATOR.PIN, e.g. IC20.51")
    des, pin = target.rsplit(".", 1)

    def net_of(b: dict) -> str | None:
        for name, rec in b["nets"].items():
            if any(nd["designator"] == des and nd["pin"] == pin
                   for nd in rec["nodes"]):
                return name
        return None

    # the same designator can exist on several boards, so a trace that starts
    # from the first one found can quietly follow the wrong signal
    shown, hits = boards_for(m, args, lambda b: net_of(b) is not None)
    if not hits:
        sys.exit(f"{target} is not on any net")
    scope_note(target, shown, hits)
    starts = [(b["key"], net_of(b)) for b in shown]

    link = cross_board_index(m)
    seen, queue, order = set(), list(starts), []
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        for nxt_key, nxt_net, via in link.get(cur, []):
            if (nxt_key, nxt_net) not in seen:
                queue.append((nxt_key, nxt_net))

    print(f"trace {target}")
    for key, name in order:
        b = board_of(m, key)
        rec = b["nets"][name]
        print(f"\n  {b['title']} / {name}   ({rec['domain']}, {rec['node_count']} pins)")
        for nd in sorted(rec["nodes"], key=lambda n: (n["designator"], n["pin"])):
            comp = b["components"].get(nd["designator"], {})
            mark = " <-- start" if nd["designator"] == des and nd["pin"] == pin else ""
            print(f"     {nd['designator']:<16}.{nd['pin']:<6} "
                  f"{str(nd.get('pin_name') or ''):<16} {comp.get('comment', ''):<22}"
                  f" {short(nd['sheet'])}{nc(nd)}{mark}")
        for nxt_key, nxt_net, via in link.get((key, name), []):
            print(f"     ==> {via}: {board_of(m, nxt_key)['title']} / {nxt_net}")



CHANNEL = re.compile(r"_([A-Z0-9]+)_PHASE$")


def cmd_netlist(m: dict, args) -> None:
    """
    Every connection in the design, in the most compact complete form.

    Measured on a three-board design: the same connectivity costs about
    14,000 tokens read out of system_model.json, 2,200 as a PADS netlist, and
    1,600 like this -- because a node is written once, pins are grouped under their part,
    and nothing repeats the sheet name. The whole three-board system fits in
    roughly 3,500 tokens, which is cheaper than a handful of `part` queries, so
    an agent that needs the whole circuit should read this rather than guess
    which questions to ask.
    """
    collapse = "--collapse" in args
    with_parts = "--parts" in args
    picked = [a for a in args if not a.startswith("--")]
    boards = [b for b in m["boards"]
              if not picked or any(w.lower() in b["title"].lower()
                                   or w.lower() in b["key"].lower() for w in picked)]
    if not boards:
        print(f"no board matching {picked}")
        return

    total_nets = sum(len(b["nets"]) for b in boards)
    total_nodes = sum(len(r["nodes"]) for b in boards for r in b["nets"].values())
    print(f"# {m['system']['name']}  {total_nets} nets, {total_nodes} nodes, "
          f"{len(boards)} board(s)   generated {m['generated_utc']}")
    print('# NET: PART.pins ...   pins grouped per part, comma-separated; a '
          'pin whose own designator contains a comma is quoted (PS1."15, 16"); '
          '* = No-ERC, deliberately open; (auto) = the designer never named '
          'this net')
    if collapse:
        print("# repeated channels written once as <CH>, with the instances listed")

    for b in boards:
        print("")
        print(f"## {b['title']}  ({len(b['nets'])} nets)")
        groups: dict = defaultdict(list)
        for name in b["nets"]:
            groups[CHANNEL.sub("_<CH>", name) if collapse else name].append(name)
        def body_of(net_name: str) -> dict:
            pins: dict = defaultdict(list)
            for nd in b["nets"][net_name]["nodes"]:
                des = nd["designator"]
                if collapse:
                    des = CHANNEL.sub("_<CH>", des)
                pins[des].append(str(nd["pin"]) + ("*" if nd.get("no_erc") else ""))
            return pins

        # Writing one channel and saying "x3" is only true if the three are
        # actually identical. A stuffing option on one phase would otherwise
        # vanish, so the members are compared and split apart when they differ.
        expanded: list[tuple[str, list[str]]] = []
        for generic, members in sorted(groups.items()):
            shapes = {json.dumps({k: sorted(v) for k, v in body_of(n).items()},
                                 sort_keys=True) for n in members}
            if len(members) > 1 and len(shapes) > 1:
                expanded += [(n, [n]) for n in sorted(members)]
            else:
                expanded.append((generic, members))

        for generic, members in expanded:
            rec = b["nets"][sorted(members)[0]]
            pins = body_of(sorted(members)[0])
            # Some symbols merge several pads into one logical pin, and that
            # pin is literally designated "15, 16" -- unquoted it would read
            # as two pins and turn one node into two.
            def render(des: str, plist: list) -> str:
                plain = sorted((x for x in plist if "," not in x and " " not in x),
                               key=lambda x: (len(x), x))
                odd = sorted(x for x in plist if "," in x or " " in x)
                bits = [f"{des}.{','.join(plain)}"] if plain else []
                bits += [f'{des}."{x}"' for x in odd]
                return " ".join(bits)

            body = " ".join(render(d, v) for d, v in sorted(pins.items()))
            tag = " (auto)" if rec.get("named_by") == "auto" else ""
            if len(members) > 1:
                chans = sorted(CHANNEL.search(x).group(1) for x in members
                               if CHANNEL.search(x))
                tag += f"  [x{len(members)}: {', '.join(chans)}]"
            print(f"{generic}: {body}{tag}")
        if with_parts:
            print("")
            print(f"## {b['title']} parts")
            for des, c in sorted(b["components"].items()):
                print(f"{des} {c['comment'] or ''}"
                      + (f" [{c['mpn']}]" if c.get("mpn") else ""))

    if m.get("interboard_links"):
        print("")
        print("## board-to-board")
        for lk in m["interboard_links"]:
            for w in lk["wires"]:
                if w.get("a_net") and w.get("b_net"):
                    print(f"{lk['designator']}: {w['a_net']} = {w['b_net']}")


def cmd_freshness(_m: dict, _args) -> None:
    """
    Report the cache state without repairing it.

    Every other command self-heals before answering, so this one deliberately
    does not -- otherwise there would be no way to see that the model had
    drifted, only that it no longer has.
    """
    from freshness import status
    st = status()
    print(f"model built {st.get('generated_utc') or '(never)'}")
    print()
    print("netlist verification, per board:")
    for b in _m["boards"]:
        cc = b.get("cross_check") or {}
        if cc.get("stale_golden_exported_utc"):
            state = (f"UNVERIFIED -- Altium's export "
                     f"({cc['stale_golden_exported_utc']}) predates the schematic")
        elif not str(cc.get("reference", "")).startswith("altium"):
            state = "UNVERIFIED -- no Altium-exported netlist to compare against"
        elif cc.get("only_in_netlist") or cc.get("only_in_model"):
            state = (f"DISAGREES -- {len(cc.get('only_in_netlist', []))} node(s) "
                     f"only in Altium's netlist, "
                     f"{len(cc.get('only_in_model', []))} only in the model")
        else:
            state = (f"verified against Altium's own netlist "
                     f"({cc.get('netlist_nodes', '?')} nodes)")
        if b.get("duplicate_designators"):
            state += (f"; {len(b['duplicate_designators'])} duplicated designator(s) "
                      f"are outside any check")
        print(f"  {b['title']:<16} {state}")
    print()
    if st["fresh"]:
        print("state: current -- matches every design file on disk")
        print("\nNote: an unsaved edit still open in Altium is not on disk and "
              "cannot be seen here.")
        return
    print(f"state: {st['reason']}")
    for label in ("changed", "added", "removed", "missing"):
        for p in st[label]:
            print(f"  {label.upper():<8} {p}")
    print("\nAny other query rebuilds this automatically; "
          "python freshness.py --rebuild does it now.")


COMMANDS = {
    "summary": cmd_summary, "issues": cmd_issues, "part": cmd_part,
    "net": cmd_net, "nets": cmd_nets, "block": cmd_block, "link": cmd_link,
    "find": cmd_find, "trace": cmd_trace, "freshness": cmd_freshness,
    "netlist": cmd_netlist,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    argv, ref = project.take_project_arg(argv)
    project.use(ref)
    cmd, args = argv[0], argv[1:]
    # `Control/C1` is the explicit form of `C1 Control`: a designator is only
    # unique within one board, and this is how you say which one you meant
    # without renaming what Altium and the silkscreen call the part.
    if args and "/" in args[0] and cmd in ("part", "nets", "net", "trace"):
        board_hint, args[0] = args[0].split("/", 1)
        args = [args[0], board_hint, *args[1:]]
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(__doc__)
        return 2
    if cmd not in ("summary", "issues", "freshness", "netlist") and not args:
        sys.exit(f"{cmd} needs an argument")
    # `freshness` reports on the cache, so it must not silently repair it first
    model = load(auto_refresh=cmd != "freshness")
    print_caveats()
    fn(model, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
