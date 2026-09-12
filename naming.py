#!/usr/bin/env python3
"""
Readable short names, derived from the design rather than hard-coded.

Sheet files in one project usually share a long prefix -- every sheet of this
controller starts `TTR_QUACKDRIVE_V2_` -- and repeating it in a table or on a
diagram wastes the width that the meaningful part needs. The prefix is found by
comparison instead of being listed, so it works on a project nobody configured.
"""

from __future__ import annotations

import os


def common_prefix(names: list[str]) -> str:
    """
    The longest prefix shared by every name, cut back to a separator.

    Cutting at a separator is what stops `Sheet1`/`Sheet2` from collapsing to
    `1`/`2`: a prefix is only worth stripping when it ends at a word boundary
    the author actually put there.
    """
    names = [n for n in names if n]
    if len(names) < 2:
        return ""
    first, last = min(names), max(names)
    i = 0
    while i < len(first) and i < len(last) and first[i] == last[i]:
        i += 1
    prefix = first[:i]
    cut = max(prefix.rfind("_"), prefix.rfind("-"))
    if cut <= 0:
        return ""
    prefix = prefix[: cut + 1]
    # never leave a name empty or reduced to a fragment
    if any(len(n) - len(prefix) < 2 for n in names):
        return ""
    return prefix


def shortener(names: list[str], drop_suffix: str = ".SchDoc"):
    """Build a name -> short name function for one project's sheets."""
    base = [n[: -len(drop_suffix)] if n.endswith(drop_suffix) else n for n in names]
    prefix = common_prefix(base)

    def short(name: str) -> str:
        s = name or ""
        if s.endswith(drop_suffix):
            s = s[: -len(drop_suffix)]
        if prefix and s.startswith(prefix):
            s = s[len(prefix):]
        return s or (name or "")

    return short


def env_flag(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)
