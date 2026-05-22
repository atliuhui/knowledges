"""Cross-platform non-blocking file lock for KB step tools.

The lock is acquired independently by each step (scan / convert / ingest) so
that any two of them 鈥?whether triggered by the CLI, the MCP server, the
pipeline wrapper, or a scheduler 鈥?never write to documents.csv, processed/,
index/database.sqlite, or the search indexes at the same time.

The wrapper that runs the three steps in sequence (actions/run-pipeline.ps1,
kb.run_pipeline) does NOT take a lock itself; it just invokes the three
independent steps, each of which acquires and releases the shared run lock.

Usage:

    from services.locking import run_lock, LockBusyError

    with run_lock(cfg, name="convert"):
        ...  # exclusive section

If another process already holds the lock, ``LockBusyError`` is raised
immediately (non-blocking). Callers decide whether to exit silently or surface
the error.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from services.config import Config


class LockBusyError(RuntimeError):
    """Raised when the shared run lock is held by another process."""


def _lock_path(cfg: Config) -> Path:
    # index/run.lock 鈥?lives next to database.sqlite. Shared by all steps so
    # that scan/convert/ingest never overlap, regardless of who invoked them.
    return cfg.database_data.parent / "run.lock"


@contextmanager
def run_lock(cfg: Config, *, name: str) -> Iterator[Path]:
    """Acquire the exclusive, non-blocking run lock for scan/convert/ingest.

    ``name`` identifies the calling step ("scan", "convert", "ingest") and is
    recorded in the lock file for diagnostics.

    Raises :class:`LockBusyError` immediately if the lock is already held.
    """
    path = _lock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open (or create) without truncating so a stale file's contents survive
    # until we successfully take the lock and overwrite them.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _acquire(fd, path)
    except LockBusyError:
        os.close(fd)
        raise

    # Write owner info for diagnostics.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        owner = (
            f"pid={os.getpid()} name={name} "
            f"started_at={datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}\n"
        )
        os.write(fd, owner.encode("utf-8"))
    except OSError:
        # Diagnostics are best-effort; do not fail the run.
        pass

    try:
        yield path
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)
            # Best-effort cleanup; ignore if another process has already grabbed it.
            try:
                path.unlink()
            except OSError:
                pass


if sys.platform == "win32":  # pragma: no cover - exercised on Windows
    import msvcrt

    def _acquire(fd: int, path: Path) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise LockBusyError(f"run lock busy: {path}") from e

    def _release(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:  # pragma: no cover - exercised on POSIX
    import fcntl

    def _acquire(fd: int, path: Path) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise LockBusyError(f"run lock busy: {path}") from e

    def _release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
