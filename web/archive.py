"""List recordings under archive/ and stream them with HTTP Range support."""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

__all__ = ["Recording", "ArchiveStore", "parse_range_header"]

# Raw session files carry a YYYYMMDD_HHMMSS_ prefix; the visible .mp4 names do not.
_SESSION_PREFIX_RE = re.compile(r"^(\d{8})_(\d{6})_")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Recording:
    creator: str
    filename: str
    size: int
    modified: datetime
    relative_path: str
    is_raw: bool
    is_merging: bool

    @property
    def modified_display(self) -> str:
        return self.modified.astimezone().strftime("%Y-%m-%d %H:%M")


def parse_range_header(header: str, file_size: int) -> Optional[tuple[int, int]]:
    """Parse a single-range Range header into an inclusive (start, end).

    Returns None when the header is absent or unparseable (caller sends the
    whole file) and raises ValueError when it is unsatisfiable (caller sends 416).
    Only bytes=... is supported; multi-range requests fall back to the full body.
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None

    if not raw_start:
        # Suffix form: the last N bytes.
        length = int(raw_end)
        if length == 0:
            raise ValueError("unsatisfiable range")
        start = max(0, file_size - length)
        end = file_size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else file_size - 1

    if start >= file_size or start > end:
        raise ValueError("unsatisfiable range")
    return start, min(end, file_size - 1)


class ArchiveStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list_recordings(self) -> List[Recording]:
        """Every .mp4/.ts per creator directory, newest first."""
        recordings: List[Recording] = []
        if not self.root.is_dir():
            return recordings

        for creator_dir in sorted(self.root.iterdir()):
            if not creator_dir.is_dir():
                continue
            for path in creator_dir.iterdir():
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".mp4", ".ts"}:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                is_merging = path.name.endswith(".merging.mp4")
                recordings.append(
                    Recording(
                        creator=creator_dir.name,
                        filename=path.name,
                        size=stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                        relative_path=f"{creator_dir.name}/{path.name}",
                        is_raw=path.suffix.lower() == ".ts",
                        is_merging=is_merging,
                    )
                )

        recordings.sort(key=lambda r: r.modified, reverse=True)
        return recordings

    def total_size(self) -> int:
        return sum(r.size for r in self.list_recordings())

    def guess_media_type(self, filename: str) -> str:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "application/octet-stream"

    @staticmethod
    def iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    @staticmethod
    def disk_usage(path: Path):
        usage = os.statvfs(path if path.exists() else path.parent)
        return usage.f_bavail * usage.f_frsize
