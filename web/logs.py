"""Tail the recorder's rotating log files for the web UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

__all__ = ["LogEntry", "read_logs"]

_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")

MAX_BYTES = 256 * 1024
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    level: str
    message: str
    source: str


def _newest_log_file(root: Path) -> Optional[Path]:
    if not root.is_dir():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix == ".log" and not path.name.endswith(".1")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_logs(root: Path, limit: int = 300, min_level: str = "DEBUG") -> List[LogEntry]:
    """Return the newest `limit` entries at or above `min_level`."""
    path = _newest_log_file(root)
    if path is None:
        return []

    threshold = LEVEL_ORDER.get(min_level.upper(), 10)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - MAX_BYTES))
            raw = handle.read()
    except OSError:
        return []

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # A seek into the middle of the file can leave a partial first line.
    if size > MAX_BYTES and lines:
        lines = lines[1:]

    entries: List[LogEntry] = []
    for line in lines:
        timestamp_match = _TIMESTAMP_RE.match(line)
        level_match = _LEVEL_RE.search(line)
        level = level_match.group(1) if level_match else "INFO"
        if LEVEL_ORDER.get(level, 20) < threshold:
            continue
        entries.append(
            LogEntry(
                timestamp=timestamp_match.group(1) if timestamp_match else "",
                level=level,
                message=line,
                source=path.name,
            )
        )

    # Entries stay in file order (oldest first) and we keep the newest `limit`.
    return entries[-limit:]


def newest_log_mtime(root: Path) -> Optional[datetime]:
    path = _newest_log_file(root)
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
