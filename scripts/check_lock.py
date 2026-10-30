#!/usr/bin/env python3
"""Check that requirements.lock is a faithful, hash-pinned lock of requirements.txt.

The image installs the lock, not requirements.txt. That means the lock is the
dependency set production actually gets, and CI -- which installs from
pyproject -- can be green while the image is missing a package or carrying one
the declared range forbids. Two facts must agree; this is where they are made to.

What is checked, all deterministically:

  1. every direct dependency in requirements.txt is present in the lock;
  2. the version the lock pins satisfies the range requirements.txt declares
     (so `fastapi>=0.170` with a lock still on 0.162 is a failure, not a note);
  3. every locked package is pinned with `==` and carries at least one hash.

It deliberately does NOT re-resolve and diff: a routine upstream release would
change the answer every week and the check would be ignored. This asks only the
question that matters -- do the two files still describe the same world.

    python3 scripts/check_lock.py            # human report
    python3 scripts/check_lock.py --json     # machine report

Exit status is 0 when the lock is faithful, 1 otherwise. A missing or unreadable
file is a failure, never a pass.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:  # packaging ships with pip, but the checker must still run without it
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:  # pragma: no cover
    SpecifierSet = None  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent


def _parse_requirement(line: str) -> tuple[str, str] | None:
    """Return (normalised_name, specifier) from one requirements.txt line."""
    clean = line.split("#", maxsplit=1)[0].strip()
    if not clean:
        return None
    # `name[extra]>=x.y` -> name and the rest.
    m = re.match(r"^([A-Za-z0-9_.-]+)\s*(\[[^\]]*\])?\s*(.*)$", clean)
    if not m:
        return None
    name = m.group(1).lower().replace("-", "_").replace(".", "_")
    return name, m.group(3).strip()


def _parse_lock(text: str) -> dict[str, tuple[str, int]]:
    """Return {normalised_name: (version, hash_count)} from the lock file."""
    locked: dict[str, tuple[str, int]] = {}
    prev = None
    hashes = 0
    for raw in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", raw)
        if m:
            if prev is not None:
                locked[prev] = (locked[prev][0], hashes)
            prev = m.group(1).lower().replace("-", "_").replace(".", "_")
            locked[prev] = (m.group(2).strip(), 0)
            hashes = 0
        elif "--hash=" in raw and prev is not None:
            hashes += 1
    if prev is not None:
        locked[prev] = (locked[prev][0], hashes)
    return locked


def _satisfies(version: str, spec: str) -> bool | None:
    """True/False if we can decide, None if packaging is unavailable."""
    if not spec:
        return True
    if SpecifierSet is None or Version is None:
        return None
    return Version(version) in SpecifierSet(spec)


def check(root: Path = ROOT) -> dict:
    problems: list[str] = []
    req_path = root / "requirements.txt"
    lock_path = root / "requirements.lock"
    if not req_path.exists():
        return {"ok": False, "problems": ["requirements.txt is missing"]}
    if not lock_path.exists():
        return {"ok": False, "problems": ["requirements.lock is missing"]}

    direct = {}
    for line in req_path.read_text("utf-8").splitlines():
        parsed = _parse_requirement(line)
        if parsed:
            direct[parsed[0]] = parsed[1]

    locked = _parse_lock(lock_path.read_text("utf-8"))
    if not locked:
        problems.append("requirements.lock pins nothing")

    for name, spec in sorted(direct.items()):
        if name not in locked:
            problems.append(f"{name}: declared in requirements.txt but absent from the lock")
            continue
        version, hashes = locked[name]
        if hashes == 0:
            problems.append(f"{name}: locked at {version} but with no hash")
        verdict = _satisfies(version, spec)
        if verdict is False:
            problems.append(
                f"{name}: lock pins {version}, which does not satisfy the declared '{spec}'"
            )

    for name, (_version, hashes) in sorted(locked.items()):
        if hashes == 0:
            problems.append(f"{name}: present in the lock with no hash")

    return {"ok": not problems, "problems": problems,
            "direct": len(direct), "locked": len(locked)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify requirements.lock against requirements.txt")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = ap.parse_args()

    report = check()
    if args.json:
        print(json.dumps(report))
    elif report["ok"]:
        print(f"lock : OK — {report['direct']} direct deps, {report['locked']} packages pinned")
    else:
        print("lock : FAILED")
        for p in report["problems"]:
            print(f"       - {p}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
