"""Shared utilities for CLI tools (logging + concurrency helpers)."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from services.config import Config
from services.locking import LockBusyError, run_lock


# Exit code used when the shared run lock is held by another process.
# 75 == EX_TEMPFAIL (BSD sysexits.h): "temporary failure; user is invited to retry".
EXIT_LOCK_BUSY = 75


@contextmanager
def acquire_run_lock_or_exit(
    cfg: Config, *, step: str, logger: logging.Logger
) -> Iterator[None]:
    """Acquire the shared run lock; exit cleanly if another step is active.

    The lock is intentionally non-blocking: when scheduled runs overlap (or a
    user kicks off a manual refresh while the MCP server is rebuilding), the
    late arrival should bow out instead of corrupting metadata/indexes.
    """
    try:
        with run_lock(cfg, name=step):
            yield
    except LockBusyError as e:
        logger.warning("run lock busy, another step is running: %s", e)
        sys.exit(EXIT_LOCK_BUSY)


def setup_logger(name: str, log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers on re-init.
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger
