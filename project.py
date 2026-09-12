#!/usr/bin/env python3
"""
Which Altium project the pipeline is currently working on, and where its
derived files live.

Nothing is hand-configured: a project is registered once from its path, and
everything after that -- boards, sheets, net-naming mode -- comes from
`discover.py`. Derived files go to a per-project cache under the user's home
directory rather than into the design folder, which is usually a git
repository and should not collect generated HTML.

  ~/.altium-view/
     index.json                 registered projects
     <slug>/raw/                extracted evidence + provenance
     <slug>/system_model.json   the model
     <slug>/*.html, schematics/ the drawings
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import discover

CACHE_ROOT = Path(
    os.environ.get("ALTIUM_VIEW_HOME") or (Path.home() / ".altium-view")
)
INDEX = CACHE_ROOT / "index.json"


class Paths:
    """Mutable holder so `use()` can retarget modules that already imported it."""

    def __init__(self):
        self.slug: str | None = None
        self.spec: dict | None = None
        self.out: Path = CACHE_ROOT
        self.raw: Path = CACHE_ROOT
        self.mbs: Path | None = None

    def __repr__(self):
        return f"<Paths {self.slug} out={self.out}>"


P = Paths()


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------

def load_index() -> dict:
    if not INDEX.exists():
        return {"projects": {}, "last": None}
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"projects": {}, "last": None}


def save_index(idx: dict) -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX)


def register(path: str | Path, remember: bool = True) -> dict:
    """Discover a project from a path and add it to the registry."""
    spec = discover.describe(path)
    idx = load_index()
    idx["projects"][spec["slug"]] = {
        "slug": spec["slug"], "title": spec["title"], "kind": spec["kind"],
        "root": spec["root"], "boards": [b["title"] for b in spec["boards"]],
        "registered_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if remember or not idx.get("last"):
        idx["last"] = spec["slug"]
    save_index(idx)
    cache_dir(spec["slug"]).mkdir(parents=True, exist_ok=True)
    (cache_dir(spec["slug"]) / "spec.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    return spec


def cache_dir(slug: str) -> Path:
    return CACHE_ROOT / slug


def resolve(ref: str | None) -> dict:
    """
    Turn a slug, a path, or nothing into a project spec.

    Nothing means the project used last, so a session that has opened one keeps
    talking about it without repeating the path.
    """
    idx = load_index()
    if ref is None:
        slug = os.environ.get("ALTIUM_VIEW_PROJECT") or idx.get("last")
        if not slug:
            raise LookupError(
                "no project selected. Register one first:\n"
                '  python altium_view.py open "<path to .PrjMbd or .PrjPcb>"')
        ref = slug

    spec_file = cache_dir(str(ref)) / "spec.json"
    if spec_file.exists():
        return json.loads(spec_file.read_text(encoding="utf-8"))
    if str(ref) in idx["projects"]:
        return register(idx["projects"][str(ref)]["root"], remember=False)
    p = Path(str(ref)).expanduser()
    if p.exists():
        return register(p, remember=False)
    known = ", ".join(sorted(idx["projects"])) or "(none)"
    raise LookupError(f"unknown project {ref!r}. Registered: {known}")


def use(ref: str | dict | None = None, remember: bool = False) -> dict:
    """
    Point the whole pipeline at a project.

    Modules hold a reference to `P` and to the `BOARDS` dict rather than to
    copies, so retargeting here reaches code that imported them earlier.

    `remember` writes the choice back to the index. Only `open` and `use` do
    that: a one-off `--project X` answers about X without quietly changing
    which project the next bare command means.
    """
    spec = ref if isinstance(ref, dict) else resolve(ref)
    P.slug, P.spec = spec["slug"], spec
    P.out = cache_dir(spec["slug"])
    P.raw = P.out / "raw"
    P.mbs = Path(spec["mbs"]) if spec.get("mbs") else None
    P.out.mkdir(parents=True, exist_ok=True)
    P.raw.mkdir(parents=True, exist_ok=True)

    import boards
    boards.BOARDS.clear()
    for b in spec["boards"]:
        boards.BOARDS[b["key"]] = {
            "title": b["title"],
            "project": Path(b["project"]),
            "flat": b["flat"],
            "sheets": [Path(s) for s in b["sheets"]],
        }
    if remember:
        idx = load_index()
        if idx.get("last") != spec["slug"]:
            idx["last"] = spec["slug"]
            save_index(idx)
    return spec


def current() -> dict:
    if P.spec is None:
        use(None)
    return P.spec


def take_project_arg(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull `--project X` out of an argument list."""
    ref = None
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--project", "-p") and i + 1 < len(argv):
            ref = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, ref
