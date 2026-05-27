"""Helpers for the H5 offline-app lane.

Agent-generated HTML5 offline apps live under ``<kb_root>/apps/<slug>/`` and
are served by a loopback miniserve instance (see
``actions/start-apps-server.ps1``). This module provides slug validation,
safe file writing and a lightweight listing API used by the
``kb.create_app`` / ``kb.list_apps`` MCP tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from services.config import Config


# Slugs must be lowercase, start with [a-z0-9], 1-64 chars, only
# [a-z0-9-] afterwards. This keeps URLs clean and forbids any path tricks.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Per-file path constraint inside an app: posix-style, no leading slash,
# no parent traversal, no drive letters, max depth 8, max segment 64 chars.
_MAX_DEPTH = 8
_MAX_SEG = 64
_SEG_RE = re.compile(r"^[A-Za-z0-9._-]{1,%d}$" % _MAX_SEG)


class AppError(ValueError):
    """Raised for any validation / safety failure in the apps lane."""


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise AppError(
            f"invalid slug {slug!r}: must match {_SLUG_RE.pattern} "
            "(lowercase a-z/0-9/'-', starts with alnum, <=64 chars)"
        )
    return slug


def _validate_rel_path(rel: str) -> Path:
    if not isinstance(rel, str) or not rel:
        raise AppError("file path must be a non-empty string")
    # Normalize separators; reject absolute paths and traversal.
    if rel.startswith(("/", "\\")) or ":" in rel:
        raise AppError(f"file path must be relative: {rel!r}")
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > _MAX_DEPTH:
        raise AppError(f"file path too deep (>{_MAX_DEPTH} segments): {rel!r}")
    for seg in parts:
        if seg in ("", ".", ".."):
            raise AppError(f"illegal path segment in {rel!r}")
        if not _SEG_RE.match(seg):
            raise AppError(
                f"illegal path segment {seg!r}: only [A-Za-z0-9._-] allowed, "
                f"<= {_MAX_SEG} chars per segment"
            )
    return Path(*parts)


def app_root(cfg: Config) -> Path:
    return cfg.apps_dir


def app_dir(cfg: Config, slug: str) -> Path:
    return app_root(cfg) / validate_slug(slug)


def _default_apps_base_url(cfg: Config) -> str:
    """Best-effort base URL when apps.base_url is not explicitly set.

    By default, start-apps-server serves ``knowledge_base_root`` and apps live
    under ``<root>/<paths.apps_dir>`` (typically ``/apps``). Mirror that path
    segment in generated links so kb.list_apps/kb.create_app URLs are directly
    clickable out of the box.
    """
    base = f"http://{cfg.apps.host}:{cfg.apps.port}".rstrip("/")
    try:
        rel = cfg.apps_dir.resolve().relative_to(cfg.knowledge_base_root.resolve())
    except ValueError:
        # Defensive fallback: if apps_dir is outside kb root for any reason,
        # return host:port without a path prefix.
        return base
    rel_posix = rel.as_posix().strip("/")
    if rel_posix and rel_posix != ".":
        return f"{base}/{rel_posix}"
    return base


def app_url(cfg: Config, slug: str) -> str:
    slug = validate_slug(slug)
    base = cfg.apps.resolved_base_url() if cfg.apps.base_url else _default_apps_base_url(cfg)
    return f"{base}/{slug}/"


@dataclass
class CreateAppResult:
    slug: str
    path: str
    url: str
    written_files: list[str]
    has_index: bool
    overwritten: bool

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "path": self.path,
            "url": self.url,
            "written_files": self.written_files,
            "has_index": self.has_index,
            "overwritten": self.overwritten,
        }


def create_app(
    cfg: Config,
    slug: str,
    files: dict[str, str],
    *,
    overwrite: bool = False,
) -> CreateAppResult:
    """Write ``files`` under ``<apps_dir>/<slug>/``.

    Each key of ``files`` is a relative path inside the app directory; each
    value is the file content (text). Existing directories are reused only
    when ``overwrite=True``; otherwise we refuse to clobber.
    """
    slug = validate_slug(slug)
    if not isinstance(files, dict) or not files:
        raise AppError("files must be a non-empty mapping of relpath -> content")

    root = app_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    target = root / slug
    overwritten = target.exists()
    if overwritten and not overwrite:
        raise AppError(
            f"app {slug!r} already exists at {target}; pass overwrite=True to replace"
        )

    # Pre-validate all paths before touching the filesystem so a bad entry
    # doesn't leave a half-written app on disk.
    resolved: list[tuple[Path, str]] = []
    for rel, content in files.items():
        if not isinstance(content, str):
            raise AppError(f"content for {rel!r} must be a string")
        rel_path = _validate_rel_path(rel)
        dest = (target / rel_path).resolve()
        # Defense in depth: ensure dest stays inside `target` after resolve.
        try:
            dest.relative_to(target.resolve())
        except ValueError as e:
            raise AppError(f"file {rel!r} escapes app directory") from e
        resolved.append((dest, content))

    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for dest, content in resolved:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(str(dest.relative_to(target)).replace("\\", "/"))

    has_index = (target / "index.html").exists()
    return CreateAppResult(
        slug=slug,
        path=str(target),
        url=app_url(cfg, slug),
        written_files=sorted(written),
        has_index=has_index,
        overwritten=overwritten,
    )


def list_apps(cfg: Config) -> list[dict]:
    """Enumerate sub-directories of ``apps_dir`` that look like an app."""
    root = app_root(cfg)
    if not root.exists():
        return []
    out: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not _SLUG_RE.match(entry.name):
            # Skip stray folders that don't match the slug convention; they
            # were not created via kb.create_app and may belong to the user.
            continue
        files = [p for p in entry.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        out.append({
            "slug": entry.name,
            "path": str(entry),
            "url": app_url(cfg, entry.name),
            "has_index": (entry / "index.html").exists(),
            "file_count": len(files),
            "size_bytes": size,
        })
    return out
