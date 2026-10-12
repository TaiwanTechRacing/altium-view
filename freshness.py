#!/usr/bin/env python3
"""
Keep the model honest: treat it as a cache of the Altium files, not as a
separate source of truth.

The design files are the only thing that is true. `system_model.json` is
derived from them, and deriving it costs about 1.3 seconds while checking
whether it is still valid costs about 4 milliseconds. So every read checks, and
rebuilds when the check fails, instead of trusting a snapshot that somebody
remembered to refresh.

Three things this has to get right:

  * A *new* sheet must count as a change. Comparing only the files recorded at
    extraction time would miss one, so the expected input set is recomputed
    from the project files every time.
  * Two sessions must not rebuild over each other. A lock file serialises it,
    and whoever loses the race just reads the result.
  * A reader must never see a half-written file, so every output is written to
    a temporary file and moved into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import project
from boards import BOARDS, sheet_paths
from project import P

ROOT = Path(__file__).resolve().parent


def provenance_path() -> Path:
    return P.raw / "provenance.json"


def lock_path() -> Path:
    return P.raw / ".rebuild.lock"
LOCK_TIMEOUT = 60.0
LOCK_STALE = 180.0

MODEL_STEPS = ["extract_offline.py", "build_system_model.py"]
DRAWING_STEPS = ["render_system.py", "render_equivalent.py", "render_schematic.py"]


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write via a temporary file so a concurrent reader sees old or new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(src.read_bytes())
    os.replace(tmp, dst)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refresh_spec() -> bool:
    """
    Re-read the board and sheet lists from the project files when any of them
    is newer than the cached spec. A sheet renamed, added or removed in Altium
    only shows up in the .PrjPcb, so trusting spec.json would look for files
    that no longer exist and miss the ones that replaced them.
    Returns True when the spec changed.
    """
    spec = P.spec
    if not spec or not spec.get("root"):
        return False
    spec_file = P.out / "spec.json"
    projects = [Path(spec["root"])] + [Path(b["project"]) for b in spec["boards"]]
    try:
        stamp = spec_file.stat().st_mtime
        if all(p.exists() and p.stat().st_mtime <= stamp for p in projects):
            return False
    except OSError:
        pass
    import discover
    try:
        new = discover.describe(spec["root"])
    except (OSError, ValueError):
        return False
    new["slug"] = spec["slug"]
    if new["boards"] == spec["boards"] and new.get("mbs") == spec.get("mbs"):
        os.utime(spec_file) if spec_file.exists() else None
        return False
    atomic_write_text(spec_file, json.dumps(new, indent=2, ensure_ascii=False))
    project.use(new)
    print("[altium-view] project file lists different sheets/boards -- spec "
          "re-discovered", file=sys.stderr)
    return True


def expected_inputs() -> list[Path]:
    """What the model should be built from, read from the projects right now."""
    out: list[Path] = []
    if P.mbs and P.mbs.exists():
        out.append(P.mbs)
    for key, cfg in BOARDS.items():
        out.append(cfg["project"])
        out += sheet_paths(key)
    return sorted({p.resolve() for p in out})


def status() -> dict:
    """Compare the recorded inputs against the files on disk."""
    if not provenance_path().exists():
        return {"fresh": False, "reason": "no provenance -- model never built",
                "changed": [], "missing": [], "added": [], "removed": []}
    prov = json.loads(provenance_path().read_text(encoding="utf-8"))
    recorded = {Path(r["path"]).resolve(): r for r in prov["inputs"]}
    current = expected_inputs()

    added = [p for p in current if p not in recorded]
    removed = [p for p in recorded if p not in set(current)]
    changed, missing = [], []
    for p in current:
        rec = recorded.get(p)
        if rec is None:
            continue
        if not p.exists():
            missing.append(p)
            continue
        st = p.stat()
        # size and mtime are the cheap gate; only hash when they disagree, so
        # the common case costs a handful of stat calls
        same_size = st.st_size == rec["bytes"]
        same_time = (datetime.fromtimestamp(st.st_mtime, timezone.utc)
                     .isoformat(timespec="seconds") == rec["modified_utc"])
        if same_size and same_time:
            continue
        if sha256(p) != rec["sha256"]:
            changed.append(p)

    fresh = not (changed or missing or added or removed)
    reason = "current"

    # Extraction records the inputs and the model is built from them in a
    # second step. If that second step fails, provenance says "current" while
    # system_model.json is still the previous one -- a freshness check that
    # only looks at the inputs would call that fresh and serve the old model
    # with no warning. Compare the two timestamps instead.
    model_file = P.out / "system_model.json"
    if fresh:
        if not model_file.exists():
            return {"fresh": False, "reason": "model missing", "changed": [],
                    "missing": [], "added": [], "removed": [],
                    "generated_utc": prov.get("generated_utc")}
        try:
            built = json.loads(model_file.read_text(encoding="utf-8")).get("generated_utc")
        except (json.JSONDecodeError, OSError):
            built = None
        if not built or (prov.get("generated_utc") or "") > built:
            return {"fresh": False,
                    "reason": "model older than the extraction it should be "
                              "built from -- a previous rebuild failed halfway",
                    "changed": [], "missing": [], "added": [], "removed": [],
                    "generated_utc": built}

    if not fresh:
        bits = []
        for label, items in (("changed", changed), ("new", added),
                             ("removed", removed), ("missing", missing)):
            if items:
                bits.append(f"{len(items)} {label}")
        reason = ", ".join(bits)
    return {"fresh": fresh, "reason": reason, "generated_utc": prov.get("generated_utc"),
            "changed": changed, "missing": missing, "added": added, "removed": removed}


class Lock:
    """A plain lock file. Whoever loses the race reads what the winner wrote."""

    def __init__(self, path: Path | None = None):
        self.path = path or lock_path()
        self.held = False

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        while time.time() < deadline:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > LOCK_STALE:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.15)
        return self          # gave up waiting; caller proceeds read-only

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)
        return False


def rebuild(drawings: bool = True, quiet: bool = False) -> bool:
    steps = MODEL_STEPS + (DRAWING_STEPS if drawings else [])
    for step in steps:
        cmd = [sys.executable, str(ROOT / step)]
        if P.slug:
            cmd += ["--project", P.slug]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        if r.returncode != 0:
            print(f"rebuild failed at {step}:\n{r.stdout}\n{r.stderr}",
                  file=sys.stderr)
            return False
        if not quiet:
            print(f"  {step} ok", file=sys.stderr)
    return True


def ensure_fresh(auto: bool = True, drawings: bool = True) -> dict:
    """
    Check, and rebuild if needed. Returns the status that was acted on.

    Set ALTIUM_VIEW_NO_REFRESH=1 to read the snapshot as-is -- useful when the
    Altium files are on a disk that is not mounted, where a failed rebuild
    would be worse than a stale answer.
    """
    if not os.environ.get("ALTIUM_VIEW_NO_REFRESH"):
        refresh_spec()
    st = status()
    if st["fresh"]:
        return {**st, "rebuilt": False}
    if os.environ.get("ALTIUM_VIEW_NO_REFRESH") or not auto:
        return {**st, "rebuilt": False}

    with Lock() as lock:
        if not lock.held:
            # someone else is rebuilding; their result is what we want
            return {**status(), "rebuilt": False,
                    "note": "another process holds the rebuild lock"}
        again = status()
        if again["fresh"]:
            return {**again, "rebuilt": False, "note": "another process rebuilt"}
        print(f"[altium-view] design files {again['reason']} -- rebuilding model",
              file=sys.stderr)
        ok = rebuild(drawings=drawings)
        return {**status(), "rebuilt": ok}


def main(argv: list[str]) -> int:
    argv, ref = project.take_project_arg(argv)
    project.use(ref)
    st = status()
    print(f"model built {st.get('generated_utc') or '(never)'}")
    print(f"state: {'current' if st['fresh'] else st['reason']}")
    for label in ("changed", "added", "removed", "missing"):
        for p in st[label]:
            print(f"  {label.upper():<8} {p}")
    if "--rebuild" in argv and not st["fresh"]:
        print()
        ensure_fresh()
    return 0 if st["fresh"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
