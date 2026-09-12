#!/usr/bin/env python3
"""
Read an Altium design without opening Altium.

Point it at a project once; after that ask it questions. Everything is derived
from the `.PrjMbd` / `.PrjPcb` / `.SchDoc` files on disk -- boards, sheets,
components, pins and nets -- and re-derived whenever those files change.

  altium_view.py open "C:/.../Thing.PrjMbd"    register a project and build it
  altium_view.py scan "C:/.../Altium"          list projects under a folder
  altium_view.py projects                      what is registered
  altium_view.py use <slug>                    switch the default project

  altium_view.py summary                       the design in ~30 lines
  altium_view.py part IC20                     one component, every pin and net
  altium_view.py net FLASH_CS                  every node on a net
  altium_view.py part Control/C1               the C1 on one named board
  altium_view.py nets IC20                     every net a component touches
  altium_view.py block ISOCAN                  one schematic sheet
  altium_view.py link C_9                      one board-to-board connector pair
  altium_view.py find ISO1042                  search parts, nets, part numbers
  altium_view.py trace U_PHASE.6               follow a signal, across boards
  altium_view.py netlist [--collapse]          every connection, compact
  altium_view.py issues                        what a human should look at
  altium_view.py freshness                     cache state vs the design files
  altium_view.py drawings                      where the HTML views are
  altium_view.py rebuild                       force a rebuild now
  altium_view.py validate                      diff against Altium's own netlist

Every command takes `--project <slug or path>`; without it, the last project
used. Queries re-check the design files first and rebuild if they moved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import project
from project import P

ROOT = Path(__file__).resolve().parent


def run(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(ROOT / script), *args]
    if P.slug:
        cmd += ["--project", P.slug]
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


# --------------------------------------------------------------------------

def cmd_open(args: list[str]) -> int:
    if not args:
        sys.exit('open needs a path: altium_view.py open "C:/.../Thing.PrjMbd"')
    spec = project.register(args[0])
    project.use(spec, remember=True)
    print(f"{spec['title']}   [{spec['kind']}]   slug={spec['slug']}")
    print(f"  source: {spec['root']}")
    for b in spec["boards"]:
        print(f"  {b['title']:<20} {len(b['sheets'])} sheet(s)  "
              f"{'flat' if b['flat'] else 'hierarchical'}")
    print(f"  cache : {P.out}")
    print("\nbuilding...")
    from freshness import rebuild
    if not rebuild():
        return 1
    print()
    return run("query.py", ["summary"])


def cmd_scan(args: list[str]) -> int:
    import discover
    folder = args[0] if args else "."
    found = discover.scan(folder)
    if not found:
        print(f"no Altium projects under {folder}")
        return 1
    for p in found:
        print(f"{p.suffix[1:]:<7} {p}")
    print(f"\n{len(found)} project(s). Register one with: "
          'altium_view.py open "<path>"')
    return 0


def cmd_projects(_args: list[str]) -> int:
    idx = project.load_index()
    if not idx["projects"]:
        print('nothing registered yet. altium_view.py open "<path to project>"')
        return 1
    for slug, rec in sorted(idx["projects"].items()):
        mark = "*" if slug == idx.get("last") else " "
        built = (project.cache_dir(slug) / "system_model.json").exists()
        print(f"{mark} {slug:<28} {rec['kind']:<11} "
              f"{'built' if built else 'not built':<10} {rec['root']}")
    print("\n* = default for commands without --project")
    return 0


def cmd_use(args: list[str]) -> int:
    if not args:
        sys.exit("use needs a slug; see: altium_view.py projects")
    spec = project.use(args[0], remember=True)
    print(f"default project is now {spec['slug']} ({spec['title']})")
    return 0


def cmd_drawings(_args: list[str]) -> int:
    names = [("equivalent_circuit.html", "the whole design as one circuit"),
             ("system_diagram.html", "board-level block diagram"),
             ("schematics/index.html", "the sheets as drawn"),
             ("system_model.md", "the model as text"),
             ("raw/provenance.json", "every source file, with hashes")]
    print(f"{P.spec['title']}  ->  {P.out}\n")
    for name, what in names:
        path = P.out / name
        mark = " " if path.exists() else "!"
        print(f"{mark} {str(path):<74} {what}")
    missing = [n for n, _ in names if not (P.out / n).exists()]
    if missing:
        print("\n! missing -- run: altium_view.py rebuild")
    # a drawing older than the model was left behind by a rebuild that failed
    # after the model was written, and it shows a design that no longer exists
    model = P.out / "system_model.json"
    if model.exists():
        behind = [n for n, _ in names
                  if (P.out / n).exists()
                  and (P.out / n).stat().st_mtime < model.stat().st_mtime - 2]
        if behind:
            print("")
            print("! older than the model, so out of date: " + ", ".join(behind))
            print("  run: altium_view.py rebuild")
    return 0


def cmd_rebuild(args: list[str]) -> int:
    from freshness import rebuild
    return 0 if rebuild(drawings="--model-only" not in args) else 1


QUERY_COMMANDS = {"summary", "part", "net", "nets", "block", "link",
                  "find", "trace", "issues", "freshness", "netlist"}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    argv, ref = project.take_project_arg(argv)
    cmd, args = argv[0], argv[1:]

    if cmd == "open":
        return cmd_open(args)
    if cmd == "scan":
        return cmd_scan(args)
    if cmd == "projects":
        return cmd_projects(args)

    try:
        project.use(ref)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2

    if cmd == "use":
        return cmd_use(args)
    if cmd == "drawings":
        return cmd_drawings(args)
    if cmd == "rebuild":
        return cmd_rebuild(args)
    if cmd == "validate":
        return run("validate_offline.py", args)
    if cmd in QUERY_COMMANDS:
        return run("query.py", [cmd, *args])

    print(f"unknown command {cmd!r}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
