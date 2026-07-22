"""Small, dependency-free helpers for safe local file persistence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_private(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a text file after writing it with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)