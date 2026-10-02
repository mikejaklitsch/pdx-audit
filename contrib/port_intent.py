#!/usr/bin/env python3
"""pdx-lint check: every deviation from vanilla needs a rule or an entry.

Copy this file into the mod's tools/lint/ folder. It runs
`pdx-audit intent check`, which reads the intent store of pdx-audit (per user,
never in the mod). A deviation that is new or changed since the store baseline,
and that no rule and no recorded entry explains, is a finding. So are a banned
name, a stale or lost entry, and two rules that disagree. Old deviations wait
until they change, or until a patch conflicts on them.

The check needs pdx-audit on the PATH. It gives the changed files to pdx-audit,
which compares only the copies whose text changed (a per-copy cache)."""

import json
import subprocess
from pathlib import Path

SUFFIXES = (".txt", ".gui")


def run(mod_root: Path, changed: set | None = None) -> list[str]:
    cmd = ["pdx-audit", "intent", "--mod-root", str(mod_root), "--json", "check"]
    if changed is not None:
        rels = sorted({Path(p).resolve().relative_to(Path(mod_root).resolve()).as_posix()
                       for p in changed if str(p).endswith(SUFFIXES)
                       and Path(p).resolve().is_relative_to(Path(mod_root).resolve())})
        if not rels:
            return []
        cmd += ["--changed", *rels]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"pdx-audit intent check did not run: {e}"]
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return [f"pdx-audit intent check failed: {(r.stderr or r.stdout).strip()[-400:]}"]
    return data.get("lines", [])
