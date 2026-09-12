#!/usr/bin/env python3
"""
The boards of whichever project is currently selected.

This used to hard-code one design. It is now a view onto the project that
`project.use()` selected, discovered from the project file rather than typed
in. `BOARDS` is deliberately a module-level dict mutated in place: modules that
did `from boards import BOARDS` keep working when the selection changes.

  python boards.py [--project <slug or path>]
"""

from __future__ import annotations

import sys
from pathlib import Path

# key -> {title, project: Path, flat: bool, sheets: [Path]}
BOARDS: dict[str, dict] = {}


def sheet_paths(board_key: str) -> list[Path]:
    return list(BOARDS[board_key]["sheets"])


def titles() -> dict[str, str]:
    return {k: v["title"] for k, v in BOARDS.items()}


def mbs() -> Path | None:
    from project import P
    return P.mbs


def main(argv: list[str]) -> int:
    import project
    argv, ref = project.take_project_arg(argv)
    spec = project.use(ref)
    print(f"{spec['title']}   [{spec['kind']}]   slug={spec['slug']}")
    print(f"  source : {spec['root']}")
    print(f"  cache  : {project.P.out}")
    if spec.get("mbs"):
        print(f"  mbs    : {Path(spec['mbs']).name}")
    for key, cfg in BOARDS.items():
        print(f"  {cfg['title']:<18} {key}  flat={cfg['flat']}  "
              f"{len(cfg['sheets'])} sheet(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
