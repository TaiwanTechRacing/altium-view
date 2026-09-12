#!/usr/bin/env python3
"""
Install the altium-view skill so an agent can use this tool from any session.

    python install.py              # for this user, every project
    python install.py --project    # only the repository you are standing in
    python install.py --check      # say what is installed, change nothing

The skill has to name the tool by absolute path -- an agent working in a
firmware repo three directories away still has to find it -- so the path is
written in at install time from wherever this file actually sits.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "skill" / "SKILL.md"
NAME = "altium-view-sch"


def target(project: bool) -> Path:
    base = Path.cwd() / ".claude" if project else Path.home() / ".claude"
    return base / "skills" / NAME / "SKILL.md"


def render() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    return text.replace("{{TOOL}}", str(ROOT / "altium_view.py").replace("\\", "/"))


def check() -> int:
    print(f"tool      {ROOT / 'altium_view.py'}")
    try:
        import olefile                                   # noqa: F401
        print("olefile   installed")
    except ImportError:
        print("olefile   MISSING -- run: pip install olefile")
    found = False
    for project in (False, True):
        path = target(project)
        if path.exists():
            found = True
            scope = "project" if project else "user"
            current = str(ROOT / "altium_view.py").replace("\\", "/") in \
                path.read_text(encoding="utf-8")
            print(f"skill     {path}  [{scope}]"
                  + ("" if current else "  -- points somewhere else, re-run install"))
    if not found:
        print("skill     not installed -- run: python install.py")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check()
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1
    try:
        import olefile                                   # noqa: F401
    except ImportError:
        print("olefile is required: pip install olefile", file=sys.stderr)
        return 1

    path = target("--project" in argv)
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF, so the installed copy stays byte-identical to the one in the repo
    path.write_text(render(), encoding="utf-8", newline=chr(10))
    print(f"installed {NAME} -> {path}")
    print(f"tool      {ROOT / 'altium_view.py'}")
    print()
    print("Register a design, then ask it anything:")
    print(f'  python "{ROOT / "altium_view.py"}" open "<path to .PrjMbd or .PrjPcb>"')
    print(f'  python "{ROOT / "altium_view.py"}" summary')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
