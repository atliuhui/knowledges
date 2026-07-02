"""Helpers for the H5 offline-app lane.

Agent-generated HTML5 offline apps live under ``<kb_root>/apps/<slug>/`` and
are served by a loopback miniserve instance (see
``actions/start-miniserve.ps1`` or ``actions/start-pocketbase.ps1``). This module provides slug validation,
safe file writing and a lightweight listing API used by the
``kb_create_app`` / ``kb_list_apps`` / ``kb_delete_app`` MCP tools.

At the apps root we also maintain two generated files that act as a
landing page for the served apps:

* ``index.json`` — machine-readable registry mutated on create/delete.
* ``index.html`` — static viewer that renders ``index.json`` in a browser.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.config import Config


# Slugs must be lowercase, start with [a-z0-9], 1-64 chars, only
# [a-z0-9-] afterwards. This keeps URLs clean and forbids any path tricks.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Per-file path constraint inside an app: posix-style, no leading slash,
# no parent traversal, no drive letters, max depth 8, max segment 64 chars.
_MAX_DEPTH = 8
_MAX_SEG = 64
_SEG_RE = re.compile(r"^[A-Za-z0-9._-]{1,%d}$" % _MAX_SEG)

# Names of the auto-managed registry / viewer files at the apps root.
INDEX_JSON_NAME = "index.json"
INDEX_HTML_NAME = "index.html"
_RESERVED_ROOT_NAMES = frozenset({INDEX_JSON_NAME, INDEX_HTML_NAME})


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

    By default, the local web server (PocketBase or miniserve) serves ``knowledge_base_root`` and apps live
    under ``<root>/<paths.apps_dir>`` (typically ``/apps``). Mirror that path
    segment in generated links so kb_list_apps/kb_create_app URLs are directly
    clickable out of the box.
    """
    # 0.0.0.0 / :: 只是 bind 通配，不是可点击的 URL；落到聊天卡片里时换成 localhost。
    host = cfg.apps.host
    if host in ("0.0.0.0", "::", "*", ""):
        host = "localhost"
    base = f"http://{host}:{cfg.apps.port}".rstrip("/")
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
    title: str

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "path": self.path,
            "url": self.url,
            "written_files": self.written_files,
            "has_index": self.has_index,
            "overwritten": self.overwritten,
            "title": self.title,
        }


def create_app(
    cfg: Config,
    slug: str,
    files: dict[str, str],
    *,
    overwrite: bool = False,
    title: str | None = None,
) -> CreateAppResult:
    """Write ``files`` under ``<apps_dir>/<slug>/``.

    Each key of ``files`` is a relative path inside the app directory; each
    value is the file content (text). Existing directories are reused only
    when ``overwrite=True``; otherwise we refuse to clobber. The app is
    registered (or updated) in ``<apps_dir>/index.json`` and the viewer
    ``<apps_dir>/index.html`` is refreshed.
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
    resolved_title = _resolve_title(title, files, slug)
    _upsert_index_entry(cfg, slug, title=resolved_title)
    return CreateAppResult(
        slug=slug,
        path=str(target),
        url=app_url(cfg, slug),
        written_files=sorted(written),
        has_index=has_index,
        overwritten=overwritten,
        title=resolved_title,
    )


def delete_app(cfg: Config, slug: str) -> dict:
    """Remove ``<apps_dir>/<slug>/`` and drop the entry from ``index.json``.

    Returns a summary dict indicating whether the directory / index entry
    were actually present. Missing artefacts are not an error so the
    operation is safely idempotent.
    """
    slug = validate_slug(slug)
    target = app_dir(cfg, slug)
    existed = target.exists()
    if existed:
        if not target.is_dir():
            raise AppError(f"path for slug {slug!r} is not a directory: {target}")
        shutil.rmtree(target)
    removed_from_index = _remove_index_entry(cfg, slug)
    return {
        "slug": slug,
        "path": str(target),
        "existed": existed,
        "removed_from_index": removed_from_index,
    }


def list_apps(cfg: Config) -> list[dict]:
    """Enumerate sub-directories of ``apps_dir`` that look like an app."""
    root = app_root(cfg)
    if not root.exists():
        return []
    titles = {entry.get("slug"): entry.get("title")
              for entry in _load_index(cfg).get("apps", [])
              if isinstance(entry, dict)}
    out: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not _SLUG_RE.match(entry.name):
            # Skip stray folders that don't match the slug convention; they
            # were not created via kb_create_app and may belong to the user.
            continue
        stats = _app_stats(cfg, entry.name)
        stats["title"] = titles.get(entry.name) or entry.name
        out.append(stats)
    return out


# ---------- registry / viewer helpers ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _index_json_path(cfg: Config) -> Path:
    return app_root(cfg) / INDEX_JSON_NAME


def _index_html_path(cfg: Config) -> Path:
    return app_root(cfg) / INDEX_HTML_NAME


def _load_index(cfg: Config) -> dict:
    p = _index_json_path(cfg)
    if not p.exists():
        return {"version": 1, "apps": [], "updated_at": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "apps": [], "updated_at": None}
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        return {"version": 1, "apps": [], "updated_at": None}
    return data


def _save_index(cfg: Config, data: dict) -> None:
    root = app_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    data.setdefault("version", 1)
    p = _index_json_path(cfg)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _app_stats(cfg: Config, slug: str) -> dict:
    target = app_dir(cfg, slug)
    files = [p for p in target.rglob("*") if p.is_file()] if target.exists() else []
    size = sum(p.stat().st_size for p in files)
    return {
        "slug": slug,
        "path": str(target),
        "url": app_url(cfg, slug),
        "has_index": (target / "index.html").exists(),
        "file_count": len(files),
        "size_bytes": size,
    }


# Keys stripped from entries before persisting to index.json. The on-disk
# absolute path is only useful for CLI/MCP consumers and would leak user
# home paths through the served viewer, so keep it in runtime responses
# only.
_INDEX_ENTRY_EXCLUDE = frozenset({"path"})


def _persistable_entry(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in _INDEX_ENTRY_EXCLUDE}


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _resolve_title(explicit: str | None, files: dict[str, str], slug: str) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    html = files.get("index.html") if isinstance(files, dict) else None
    if isinstance(html, str):
        m = _TITLE_RE.search(html)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
    return slug


def _upsert_index_entry(cfg: Config, slug: str, *, title: str) -> dict:
    data = _load_index(cfg)
    apps_raw = data.get("apps") or []
    existing = next(
        (a for a in apps_raw if isinstance(a, dict) and a.get("slug") == slug),
        None,
    )
    entry = _app_stats(cfg, slug)
    entry["title"] = title
    now = _now_iso()
    entry["created_at"] = (existing or {}).get("created_at") or now
    entry["updated_at"] = now
    remaining = [
        a for a in apps_raw
        if isinstance(a, dict) and a.get("slug") != slug
    ]
    remaining.append(_persistable_entry(entry))
    remaining.sort(key=lambda a: a.get("slug", ""))
    data["apps"] = remaining
    _save_index(cfg, data)
    _write_index_html(cfg)
    return entry


def _remove_index_entry(cfg: Config, slug: str) -> bool:
    data = _load_index(cfg)
    apps_raw = data.get("apps") or []
    kept = [
        a for a in apps_raw
        if isinstance(a, dict) and a.get("slug") != slug
    ]
    changed = len(kept) != len(apps_raw)
    data["apps"] = kept
    _save_index(cfg, data)
    _write_index_html(cfg)
    return changed


def rebuild_index(cfg: Config) -> dict:
    """Rescan ``apps_dir`` and rewrite ``index.json`` / ``index.html``.

    Preserves ``created_at`` for slugs already present in the previous
    ``index.json`` so history isn't lost when the registry is regenerated.
    When an existing entry has no title, tries to read one from the app's
    own ``index.html`` before falling back to the slug.
    """
    root = app_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    prev = {
        a.get("slug"): a
        for a in _load_index(cfg).get("apps", [])
        if isinstance(a, dict)
    }
    now = _now_iso()
    apps: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not _SLUG_RE.match(entry.name):
            continue
        stats = _app_stats(cfg, entry.name)
        old = prev.get(entry.name) or {}
        old_title = old.get("title")
        # Treat "title == slug" as a placeholder from a prior bare rebuild so
        # we can upgrade to a real <title> once the app has an index.html.
        if not old_title or old_title == entry.name:
            old_title = None
        stats["title"] = old_title or _title_from_dir(entry) or entry.name
        stats["created_at"] = old.get("created_at") or now
        stats["updated_at"] = now
        apps.append(_persistable_entry(stats))
    data = {"version": 1, "apps": apps, "updated_at": now}
    _save_index(cfg, data)
    _write_index_html(cfg)
    return data


def _title_from_dir(app_dir_path: Path) -> str | None:
    """Best-effort title extraction from ``<app_dir>/index.html``."""
    index = app_dir_path / "index.html"
    if not index.is_file():
        return None
    try:
        # Read a bounded prefix; <title> is expected inside <head>.
        with index.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return None
    m = _TITLE_RE.search(head)
    if not m:
        return None
    candidate = m.group(1).strip()
    return candidate or None


def _write_index_html(cfg: Config) -> None:
    p = _index_html_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_INDEX_HTML_TEMPLATE, encoding="utf-8")


_INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Knowledge Base Apps</title>
    <style>
      :root {
        color-scheme: light dark;
        --bg: #f5f7fb;
        --card: #ffffff;
        --text: #1c2333;
        --muted: #667085;
        --accent: #0a7f5a;
        --border: #e5e9f0;
      }
      @media (prefers-color-scheme: dark) {
        :root {
          --bg: #0f1420;
          --card: #171d2b;
          --text: #e6ebf5;
          --muted: #98a2b3;
          --border: #232a3b;
        }
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        background: var(--bg);
        color: var(--text);
      }
      header {
        padding: 32px 24px 8px;
        max-width: 960px;
        margin: 0 auto;
      }
      header h1 { margin: 0 0 4px; font-size: 24px; }
      header p { margin: 0; color: var(--muted); font-size: 14px; }
      main {
        max-width: 960px;
        margin: 16px auto 48px;
        padding: 0 24px;
        display: grid;
        gap: 12px;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04);
      }
      .card h2 {
        margin: 0;
        font-size: 16px;
        overflow-wrap: anywhere;
      }
      .card a.slug {
        color: var(--accent);
        text-decoration: none;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 13px;
      }
      .card a.slug:hover { text-decoration: underline; }
      .meta {
        display: flex;
        flex-wrap: wrap;
        gap: 8px 14px;
        color: var(--muted);
        font-size: 12px;
      }
      .meta-row {
        color: var(--muted);
        font-size: 12px;
      }
      .meta code {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }
      .empty, .error {
        grid-column: 1 / -1;
        text-align: center;
        color: var(--muted);
        padding: 32px 12px;
        border: 1px dashed var(--border);
        border-radius: 12px;
      }
      .error { color: #b42318; }
      footer {
        max-width: 960px;
        margin: 0 auto 32px;
        padding: 0 24px;
        color: var(--muted);
        font-size: 12px;
        text-align: right;
      }
    </style>
  </head>
  <body>
    <header>
      <h1>Knowledge Base Apps</h1>
      <p>本页面读取同目录下的 <code>index.json</code>，由 <code>kb_create_app</code> / <code>kb_delete_app</code> 自动维护。</p>
    </header>
    <main id="grid" aria-live="polite">
      <div class="empty">Loading…</div>
    </main>
    <footer id="footer"></footer>
    <script>
      (function () {
        const grid = document.getElementById('grid');
        const footer = document.getElementById('footer');
        const fmtBytes = (n) => {
          if (!Number.isFinite(n)) return '';
          const units = ['B', 'KB', 'MB', 'GB'];
          let i = 0;
          while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
          return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
        };
        const escape = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        })[c]);
        fetch('./index.json', { cache: 'no-store' })
          .then((r) => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
          })
          .then((data) => {
            const apps = Array.isArray(data.apps) ? data.apps : [];
            if (apps.length === 0) {
              grid.innerHTML = '<div class="empty">还没有已注册的 app。</div>';
              return;
            }
            grid.innerHTML = apps.map((a) => {
              const title = escape(a.title || a.slug);
              const slug = escape(a.slug || '');
              const href = `./${encodeURIComponent(slug)}/`;
              const files = Number.isFinite(a.file_count) ? `${a.file_count} 文件` : '';
              const size = fmtBytes(a.size_bytes);
              const updated = a.updated_at ? new Date(a.updated_at).toLocaleString() : '';
              const missingIndex = a.has_index === false
                ? '<span title="缺少 index.html">⚠ no index.html</span>'
                : '';
              return `
                <article class="card">
                  <h2>${title}</h2>
                  <a class="slug" href="${href}" target="_blank" rel="noopener"><code>${slug}</code></a>
                  <div class="meta">
                    ${files ? `<span>${files}</span>` : ''}
                    ${size ? `<span>${size}</span>` : ''}
                  </div>
                  ${updated ? `<div class="meta-row">updated ${escape(updated)}</div>` : ''}
                  ${missingIndex ? `<div class="meta-row">${missingIndex}</div>` : ''}
                </article>
              `;
            }).join('');
            if (data.updated_at) {
              footer.textContent = `index.json updated ${new Date(data.updated_at).toLocaleString()}`;
            }
          })
          .catch((err) => {
            grid.innerHTML = `<div class="error">Failed to load index.json: ${escape(err.message || err)}</div>`;
          });
      })();
    </script>
  </body>
</html>
"""
