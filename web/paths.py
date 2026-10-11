"""Host directory layout and safe path resolution.

The web process never trusts a path that came from a browser: every request path
is resolved against a fixed root and must stay inside it.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "HOST_ROOT",
    "archive_root",
    "config_file",
    "env_file",
    "logs_root",
    "resolve_within",
]

# The compose service mounts the project directory at /host, so a single mount
# covers .env, config/, archive/ and logs/ without create_host_path footguns.
HOST_ROOT = Path(os.environ.get("RPLAY_HOST_ROOT", "/host")).resolve()

ARCHIVE_SUBDIR = "archive"
CONFIG_SUBDIR = "config"
LOGS_SUBDIR = "logs"
ENV_FILENAME = ".env"
CONFIG_FILENAME = "config.yaml"


def archive_root() -> Path:
    return HOST_ROOT / ARCHIVE_SUBDIR


def logs_root() -> Path:
    return HOST_ROOT / LOGS_SUBDIR


def env_file() -> Path:
    return HOST_ROOT / ENV_FILENAME


def config_file() -> Path:
    return HOST_ROOT / CONFIG_SUBDIR / CONFIG_FILENAME


def resolve_within(root: Path, *parts: str) -> Path | None:
    """Join parts onto root and return the result only if it stays inside root.

    Returns None for traversal attempts, absolute parts, and symlinks that point
    outside the root. Callers must treat None as "bad request".
    """
    root = root.resolve()
    candidate = root
    for part in parts:
        if not part or part in {".", ".."}:
            return None
        # Reject separators and absolute paths before joining, so a part can
        # never introduce a new hierarchy level.
        if "/" in part or "\\" in part or part.startswith("."):
            return None
        candidate = candidate / part

    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved
