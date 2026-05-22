"""Document editing service backing the MCP `kb.*_document_update` tools.

Only human-maintained fields may be edited; tool-maintained fields are rejected.
Workflow: callers must `preview` first, then `apply` after user confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import documents as dc
from .config import Config


# Fields that trigger an index metadata refresh when changed (ingest required).
INDEX_AFFECTING = {"title", "tags", "confidentiality", "status", "type"}


class DocumentEditError(ValueError):
    pass


@dataclass
class FieldChange:
    before: Any
    after: Any


@dataclass
class DocChange:
    id: str
    source_path: str
    changes: dict[str, FieldChange] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "before": {k: v.before for k, v in self.changes.items()},
            "after": {k: v.after for k, v in self.changes.items()},
        }


def _validate_patch(patch: dict[str, Any]) -> None:
    bad = set(patch.keys()) - dc.HUMAN_FIELDS
    if bad:
        raise DocumentEditError(f"cannot modify tool-maintained fields: {sorted(bad)}")
    if "status" in patch and patch["status"] and patch["status"] not in dc.ALLOWED_STATUSES:
        raise DocumentEditError(f"invalid status: {patch['status']}")
    if (
        "confidentiality" in patch
        and patch["confidentiality"] not in dc.ALLOWED_CONFIDENTIALITIES
    ):
        raise DocumentEditError(f"invalid confidentiality: {patch['confidentiality']}")


def _normalize_tags(value: Any) -> str:
    if isinstance(value, list):
        seen: dict[str, None] = {}
        for t in value:
            t = str(t).strip()
            if t:
                seen.setdefault(t, None)
        return ";".join(seen.keys())
    return str(value or "").strip()


def _diff(row: dc.DocumentRow, patch: dict[str, Any]) -> dict[str, FieldChange]:
    changes: dict[str, FieldChange] = {}
    for k, v in patch.items():
        new_val = _normalize_tags(v) if k == "tags" else (v if v is not None else "")
        old_val = getattr(row, k)
        if k == "tags":
            old_norm = old_val
            new_norm = new_val
        else:
            old_norm = old_val
            new_norm = str(new_val)
        if old_norm != new_norm:
            before = row.tag_list() if k == "tags" else old_val
            after = [t for t in new_val.split(";") if t] if k == "tags" else new_val
            changes[k] = FieldChange(before=before, after=after)
    return changes


def preview_update(cfg: Config, doc_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    _validate_patch(patch)
    rows = dc.load_csv(cfg.documents_data)
    row = next((r for r in rows if r.id == doc_id), None)
    if row is None:
        raise DocumentEditError(f"document not found: {doc_id}")
    change = DocChange(id=row.id, source_path=row.source_path, changes=_diff(row, patch))
    requires_ingest = any(k in INDEX_AFFECTING for k in change.changes.keys())
    return {
        "changes": [change.as_dict()] if change.changes else [],
        "requires_ingest": requires_ingest,
        "reason": (
            "tags/confidentiality/status/title/type changed and indexes need metadata refresh"
            if requires_ingest
            else "only non-index fields changed"
        ),
    }


def apply_update(cfg: Config, doc_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    _validate_patch(patch)
    rows = dc.load_csv(cfg.documents_data)
    found = False
    requires_ingest = False
    for row in rows:
        if row.id != doc_id:
            continue
        found = True
        diff = _diff(row, patch)
        if not diff:
            break
        for k, v in patch.items():
            if k == "tags":
                row.set_tag_list(v if isinstance(v, list) else _normalize_tags(v).split(";"))
            else:
                setattr(row, k, "" if v is None else str(v))
        if any(k in INDEX_AFFECTING for k in diff.keys()):
            requires_ingest = True
        break
    if not found:
        raise DocumentEditError(f"document not found: {doc_id}")
    dc.save_csv(cfg.documents_data, rows)
    return {"updated": 1, "requires_ingest": requires_ingest}


@dataclass
class BulkOperation:
    add_tags: list[str] = field(default_factory=list)
    remove_tags: list[str] = field(default_factory=list)
    set_confidentiality: str | None = None
    set_status: str | None = None
    set_type: str | None = None


def _apply_bulk(row: dc.DocumentRow, op: BulkOperation) -> dict[str, FieldChange]:
    diff: dict[str, FieldChange] = {}
    if op.add_tags or op.remove_tags:
        before = row.tag_list()
        tags = [t for t in before if t not in set(op.remove_tags)]
        for t in op.add_tags:
            if t and t not in tags:
                tags.append(t)
        if tags != before:
            diff["tags"] = FieldChange(before=before, after=tags)
            row.set_tag_list(tags)
    if op.set_confidentiality is not None and op.set_confidentiality != row.confidentiality:
        if op.set_confidentiality not in dc.ALLOWED_CONFIDENTIALITIES:
            raise DocumentEditError(f"invalid confidentiality: {op.set_confidentiality}")
        diff["confidentiality"] = FieldChange(row.confidentiality, op.set_confidentiality)
        row.confidentiality = op.set_confidentiality
    if op.set_status is not None and op.set_status != row.status:
        if op.set_status not in dc.ALLOWED_STATUSES:
            raise DocumentEditError(f"invalid status: {op.set_status}")
        diff["status"] = FieldChange(row.status, op.set_status)
        row.status = op.set_status
    if op.set_type is not None and op.set_type != row.type:
        diff["type"] = FieldChange(row.type, op.set_type)
        row.type = op.set_type
    return diff


def bulk_preview(cfg: Config, ids: Iterable[str], op: BulkOperation) -> dict[str, Any]:
    rows = dc.load_csv(cfg.documents_data)
    id_set = set(ids)
    changes_out: list[dict[str, Any]] = []
    requires_ingest = False
    for row in rows:
        if row.id not in id_set:
            continue
        snapshot = dc.DocumentRow(**row.__dict__)
        diff = _apply_bulk(snapshot, op)
        if diff:
            changes_out.append(
                DocChange(id=row.id, source_path=row.source_path, changes=diff).as_dict()
            )
            if any(k in INDEX_AFFECTING for k in diff.keys()):
                requires_ingest = True
    return {
        "changes": changes_out,
        "affected": len(changes_out),
        "requires_ingest": requires_ingest,
    }


def bulk_apply(cfg: Config, ids: Iterable[str], op: BulkOperation) -> dict[str, Any]:
    rows = dc.load_csv(cfg.documents_data)
    id_set = set(ids)
    updated = 0
    requires_ingest = False
    for row in rows:
        if row.id not in id_set:
            continue
        diff = _apply_bulk(row, op)
        if diff:
            updated += 1
            if any(k in INDEX_AFFECTING for k in diff.keys()):
                requires_ingest = True
    dc.save_csv(cfg.documents_data, rows)
    return {"updated": updated, "requires_ingest": requires_ingest}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
