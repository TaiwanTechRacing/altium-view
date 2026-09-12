#!/usr/bin/env python3
"""
Work out what an Altium project contains from a path alone.

Point this at a `.PrjMbd`, a `.PrjPcb`, or a folder, and it returns the boards,
their schematic sheets and each board's net-naming mode -- everything the rest
of the pipeline needs, with nothing hand-configured.

  python discover.py "C:/.../TTR_quackDrive_V2.PrjMbd"
  python discover.py "C:/Users/Public/Documents/Altium"     # scan a folder
"""

from __future__ import annotations

import configparser
import json
import re
import sys
import zipfile
from pathlib import Path

SCHEMATIC_SUFFIX = ".schdoc"


def read_ini(path: Path) -> configparser.ConfigParser:
    """Altium project files are INI with a BOM and duplicate-ish keys."""
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
    return cp


def ini_documents(path: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """DocumentPath entries with one of the given suffixes, resolved."""
    cp = read_ini(path)
    out: list[Path] = []
    for section in cp.sections():
        if not section.lower().startswith("document"):
            continue
        name = cp[section].get("DocumentPath")
        if name and name.lower().endswith(suffixes):
            out.append((path.parent / name).resolve())
    return out


def hierarchy_flat(prj: Path) -> bool:
    """
    Whether net labels are global across the whole project.

    Altium writes `HierarchyMode=0` for a flat project and a non-zero mode for
    a hierarchical one. A project with no sheet symbols is flat regardless, so
    the structure is used as a fallback when the key is absent.
    """
    cp = read_ini(prj)
    for section in cp.sections():
        if "HierarchyMode" in cp[section]:
            return cp[section]["HierarchyMode"].strip() in ("0", "flat", "Flat")
    return True


def board_titles(projects: list[Path]) -> dict[str, str]:
    """
    Readable board names, by comparison rather than by guesswork.

    Sibling boards in one design share a prefix -- strip it and
    `TTR_quackDrive_V2_ControlBoard` becomes `ControlBoard`, then `Control
    Board`. A lone board has nothing to compare against, so it keeps its stem:
    inventing words out of `..._NEW_V2` produced titles like "V 2".
    """
    from naming import common_prefix
    stems = [p.stem for p in projects]
    prefix = common_prefix(stems) if len(stems) > 1 else ""
    out = {}
    for p in projects:
        tail = p.stem[len(prefix):] if prefix and p.stem.startswith(prefix) else p.stem
        words = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|\d+", tail)
        out[p.stem] = " ".join(words) if len(words) >= 2 and prefix else tail
    return out


def mbs_titles(mbs: Path) -> dict[str, str]:
    """Board titles as the multi-board schematic names them, keyed by project stem."""
    out: dict[str, str] = {}
    try:
        with zipfile.ZipFile(mbs) as z:
            logical = json.loads(z.read("logical.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return out
    for m in logical.get("modules", []):
        src = m.get("source", {})
        name = src.get("projectName")
        if name and m.get("title"):
            out[Path(name).stem] = m["title"]
    return out


def board_spec(prj: Path, title: str | None = None) -> dict:
    sheets = [p for p in ini_documents(prj, (SCHEMATIC_SUFFIX,)) if p.exists()]
    if not sheets:
        sheets = sorted(prj.parent.glob("*.SchDoc"))
    return {
        "key": prj.stem,
        "title": title or prj.stem,
        "project": str(prj),
        "flat": hierarchy_flat(prj),
        "sheets": [str(p) for p in sheets],
    }


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def describe(path: str | Path) -> dict:
    """
    A full project spec from one path.

    Accepts the multi-board project, a single board project, or a folder
    holding either.
    """
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        found = (sorted(path.glob("*.PrjMbd")) or sorted(path.glob("*.PrjPcb"))
                 or sorted(path.glob("*/*.PrjMbd")) or sorted(path.glob("*/*.PrjPcb")))
        if not found:
            raise FileNotFoundError(f"no .PrjMbd or .PrjPcb under {path}")
        path = found[0]
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix == ".prjmbd":
        mbs_list = [p for p in ini_documents(path, (".mbsdoc",)) if p.exists()]
        mbs = mbs_list[0] if mbs_list else None
        titles = mbs_titles(mbs) if mbs else {}
        members = [p for p in ini_documents(path, (".prjpcb",)) if p.exists()]
        derived = board_titles(members)
        boards = [board_spec(p, titles.get(p.stem) or derived.get(p.stem))
                  for p in members]
        if not boards:
            raise ValueError(f"{path.name} lists no .PrjPcb members")
        return {"slug": slugify(path.stem), "title": path.stem, "kind": "multiboard",
                "root": str(path), "mbs": str(mbs) if mbs else None, "boards": boards}

    if suffix == ".prjpcb":
        return {"slug": slugify(path.stem), "title": path.stem, "kind": "single",
                "root": str(path), "mbs": None, "boards": [board_spec(path)]}

    raise ValueError(f"not an Altium project: {path.name}")


def scan(folder: str | Path) -> list[Path]:
    """Every project under a folder, multi-board ones first."""
    folder = Path(folder).expanduser().resolve()
    mbd = sorted(folder.rglob("*.PrjMbd"))
    pcb = sorted(folder.rglob("*.PrjPcb"))
    members = set()
    for m in mbd:
        try:
            members |= {p for p in ini_documents(m, (".prjpcb",))}
        except Exception:                                   # noqa: BLE001
            pass
    return mbd + [p for p in pcb if p.resolve() not in members]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    target = Path(argv[0]).expanduser()
    if target.is_dir() and "--scan" in argv:
        for p in scan(target):
            print(f"{p.suffix[1:]:<7} {p}")
        return 0
    spec = describe(target)
    print(f"{spec['title']}   [{spec['kind']}]   slug={spec['slug']}")
    if spec["mbs"]:
        print(f"  multi-board schematic: {Path(spec['mbs']).name}")
    for b in spec["boards"]:
        print(f"  {b['title']:<18} {b['key']}")
        print(f"      flat={b['flat']}   {len(b['sheets'])} sheet(s)")
        for s in b["sheets"]:
            print(f"        {Path(s).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
