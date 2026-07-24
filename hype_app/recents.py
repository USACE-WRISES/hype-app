"""Recent-projects list for the desktop startup dialog (RAS2025-style).

Persisted per-user at <data root>/recent_projects.json, where the data root mirrors the
shell's ShellConfig.Create resolution exactly: HYPE_DATA_ROOT env override, else
%LOCALAPPDATA%\\HYPE (the shell exports its own env to the app process, so the override
case propagates for free), else ~/.hype for non-Windows dev shells.

Every helper here is deliberately non-fatal: a broken or read-only data root must never
fail project adoption or the welcome dialog — worst case the list is just empty.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_FILE = "recent_projects.json"
MAX_RECENTS = 15


def data_root() -> Path:
    """The per-user desktop data root (same resolution as the C# shell's ShellConfig)."""
    override = os.environ.get("HYPE_DATA_ROOT")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "HYPE"
    return Path.home() / ".hype"


def _path() -> Path:
    return data_root() / _FILE


def load() -> list[dict]:
    """Recents newest-first, pruned of entries whose .hype file no longer exists.

    Each entry: {"path": str, "name": str, "last_opened": iso-utc str}. Pruning is
    in-memory only (the file is rewritten on the next touch), so a temporarily
    unavailable drive doesn't permanently evict its projects.
    """
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        items = raw.get("projects", [])
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    for it in items:
        p = str(it.get("path") or "")
        if not p:
            continue
        try:
            if not Path(p).is_file():
                continue
        except OSError:
            continue
        out.append({"path": p, "name": str(it.get("name") or Path(p).stem),
                    "last_opened": str(it.get("last_opened") or "")})
    return out[:MAX_RECENTS]


def touch(path: str | os.PathLike[str]) -> None:
    """Record a project open/create: dedupe by normalized path, insert front, cap, write.

    Atomic same-dir tmp + os.replace so a crash mid-write can't corrupt the list.
    Silently a no-op on any IO failure.
    """
    try:
        p = Path(path).resolve()
        key = os.path.normcase(str(p))
        entry = {"path": str(p), "name": p.stem,
                 "last_opened": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        kept = [it for it in load() if os.path.normcase(it["path"]) != key]
        items = [entry, *kept][:MAX_RECENTS]

        root = data_root()
        root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".recents-", suffix=".tmp", dir=str(root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"projects": items}, fh, indent=2)
            os.replace(tmp, _path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass


__all__ = ["MAX_RECENTS", "data_root", "load", "touch"]
