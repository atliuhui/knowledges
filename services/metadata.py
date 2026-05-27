"""Read / write `store/docs.csv`.

The CSV is the source-document state boundary. It mixes tool-maintained snapshot
fields with human-maintained semantic fields. We preserve human fields on update.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable


FIELDS = [
    # tool-maintained snapshot
    "id",
    "source_path",
    "source_hash",
    "source_size",
    "source_mtime",
    # human-maintained semantic
    "title",
    "type",
    "tags",
    "confidentiality",
    "status",
    # tool-maintained lifecycle
    "discovered_at",
    "scanned_at",
    # human-maintained
    "notes",
]

TOOL_FIELDS = {
    "id", "source_path", "source_hash", "source_size", "source_mtime",
    "discovered_at", "scanned_at",
}
HUMAN_FIELDS = {"title", "type", "tags", "confidentiality", "status", "notes"}

ALLOWED_STATUSES = {"active", "archived", "ignored", "missing"}
ALLOWED_CONFIDENTIALITIES = {"", "public", "internal", "confidential", "private"}


@dataclass
class MetadataRow:
    id: str = ""
    source_path: str = ""
    source_hash: str = ""
    source_size: str = ""
    source_mtime: str = ""
    title: str = ""
    type: str = ""
    tags: str = ""
    confidentiality: str = ""
    status: str = "active"
    discovered_at: str = ""
    scanned_at: str = ""
    notes: str = ""

    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(";") if t.strip()]

    def set_tag_list(self, tags: Iterable[str]) -> None:
        # de-dupe, preserve order
        seen: dict[str, None] = {}
        for t in tags:
            t = t.strip()
            if t:
                seen.setdefault(t, None)
        self.tags = ";".join(seen.keys())


def load_csv(path: Path) -> list[MetadataRow]:
    if not path.exists():
        return []
    rows: list[MetadataRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            kwargs = {k: (raw.get(k) or "") for k in FIELDS}
            rows.append(MetadataRow(**kwargs))
    return rows


def save_csv(path: Path, rows: list[MetadataRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: asdict(r).get(k, "") for k in FIELDS})
    tmp.replace(path)


def next_id(existing: list[MetadataRow]) -> str:
    """Generate the next stable id `kb-NNNNNN`."""
    max_n = 0
    for r in existing:
        if r.id.startswith("kb-"):
            try:
                n = int(r.id[3:])
                max_n = max(max_n, n)
            except ValueError:
                continue
    return f"kb-{max_n + 1:06d}"


def validate(rows: list[MetadataRow]) -> list[str]:
    """Return a list of validation error strings (empty = ok)."""
    errors: list[str] = []
    seen_ids: dict[str, int] = {}
    seen_paths: dict[str, int] = {}
    for idx, r in enumerate(rows, start=1):
        if not r.id:
            errors.append(f"row {idx}: empty id")
        elif r.id in seen_ids:
            errors.append(f"row {idx}: duplicate id {r.id} (also row {seen_ids[r.id]})")
        else:
            seen_ids[r.id] = idx
        if not r.source_path:
            errors.append(f"row {idx} ({r.id}): empty source_path")
        elif r.source_path in seen_paths:
            errors.append(
                f"row {idx} ({r.id}): duplicate source_path {r.source_path} "
                f"(also row {seen_paths[r.source_path]})"
            )
        else:
            seen_paths[r.source_path] = idx
        if r.status and r.status not in ALLOWED_STATUSES:
            errors.append(f"row {idx} ({r.id}): invalid status '{r.status}'")
        if r.confidentiality not in ALLOWED_CONFIDENTIALITIES:
            errors.append(
                f"row {idx} ({r.id}): invalid confidentiality '{r.confidentiality}'"
            )
    return errors
