"""Verify desktop/payload/env.lock still satisfies the app's requirements.txt.

requirements.txt uses version FLOORS (pkg>=x.y); env.lock freezes the exact Windows resolution
uv computed when it was generated. Transitive pins legitimately drift and are only refreshed
when a developer re-locks. What must NEVER drift silently is a direct requirement: every
direct dependency must be present in env.lock at a version that satisfies its specifier —
otherwise the desktop payload would ship a stack the app was never meant to run on.
Deterministic (no network, no resolution). Run via `uv run --with packaging -- python …`.

Exit 0 = consistent; exit 1 with a report otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(line)
        if match:
            pins[canonical(match.group(1))] = match.group(2)
    return pins


def parse_requirements(path: Path) -> list[Requirement]:
    requirements: list[Requirement] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement:
            print(f"  (skipping unparseable line: {line!r})")
    return requirements


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    lock = parse_lock(repo / "desktop/payload/env.lock")
    if not lock:
        print("FAIL: desktop/payload/env.lock has no pins?")
        return 1

    problems: list[str] = []
    checked = 0
    for req in parse_requirements(repo / "requirements.txt"):
        name = canonical(req.name)
        locked = lock.get(name)
        if locked is None:
            problems.append(f"{req.name} ({req.specifier}) is missing from env.lock")
            continue
        checked += 1
        try:
            if req.specifier and not req.specifier.contains(Version(locked), prereleases=True):
                problems.append(f"{req.name}: env.lock has {locked}, which fails '{req.specifier}'")
        except InvalidVersion:
            problems.append(f"{req.name}: env.lock version {locked!r} is not a valid version")

    if problems:
        print("env.lock is OUT OF SYNC with requirements.txt:")
        for problem in problems:
            print("  -", problem)
        print("\nRegenerate and commit it:")
        print("  uv pip compile requirements.txt --python-version 3.12 "
              "--python-platform windows --no-header -o desktop/payload/env.lock")
        return 1

    print(f"env.lock satisfies all {checked} direct requirements ({len(lock)} locked packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
