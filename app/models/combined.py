"""Combined decks: every video of one batch (a playlist or a dropped folder)
in a single PowerPoint / Google Slides deck.

There is one record per user and batch. It remembers the order the person
put the videos in and which ones they left out, so the next "Convert all
into one" opens the way they left it, and it holds the batch's Google deck
so converting again replaces that deck rather than making another. Export
state works exactly as on a job (app/models/jobs.py): {"pptx": {...},
"gslides": {...}} for the last run, and `gslides` for the deck itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.core.db import BUCKET_COMBINED, Record, Store
from app.models import from_iso, to_iso, utc_now
from app.models.jobs import EXPORT_DONE, EXPORT_FAILED, EXPORT_GSLIDES, EXPORT_WORKING, ERROR_MAX, clean_title


@dataclass
class Combined:
    id: int
    user_id: int
    batch: str
    title: str
    created_at: datetime
    updated_at: datetime
    # Every video in the order the person arranged them, ticked or not;
    # left_out are the unticked ones, which the deck skips.
    job_ids: list[int] = field(default_factory=list)
    left_out: list[int] = field(default_factory=list)
    exports: dict[str, dict[str, Any]] = field(default_factory=dict)
    gslides: dict[str, str] | None = None

    @property
    def gslides_file_id(self) -> str | None:
        return (self.gslides or {}).get("file_id") or None

    @property
    def gslides_url(self) -> str | None:
        return (self.gslides or {}).get("url") or None

    def to_record(self) -> Record:
        record = asdict(self)
        for key in ("created_at", "updated_at"):
            record[key] = to_iso(getattr(self, key))
        return record

    @classmethod
    def from_record(cls, record: Record) -> "Combined":
        values = dict(record)
        for key in ("created_at", "updated_at"):
            values[key] = from_iso(values.get(key))
        return cls(**values)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch": self.batch,
            "title": self.title,
            "job_ids": self.job_ids,
            "left_out": self.left_out,
            "exports": self.exports,
            "gslides_url": self.gslides_url,
        }


def get(store: Store, combined_id: int) -> Combined | None:
    record = store.get(BUCKET_COMBINED, combined_id)
    return None if record is None else Combined.from_record(record)


def list_for_user(store: Store, user_id: int) -> list[Combined]:
    found = (Combined.from_record(record) for _, record in store.all(BUCKET_COMBINED))
    return [combo for combo in found if combo.user_id == user_id]


def find(store: Store, user_id: int, batch: str) -> Combined | None:
    return next((combo for combo in list_for_user(store, user_id) if combo.batch == batch), None)


def delete(store: Store, combined_id: int) -> None:
    store.delete(BUCKET_COMBINED, combined_id)


def save_choice(store: Store, user_id: int, batch: str, title: str, job_ids: list[int], left_out: list[int]) -> Combined:
    """Create the batch's record, or update its title, order and left-out
    videos. The deck and the export state are kept."""
    existing = find(store, user_id, batch)
    if existing is None:
        now = utc_now()
        combo = Combined(id=store.next_sequence(BUCKET_COMBINED), user_id=user_id, batch=batch,
                         title=clean_title(title), created_at=now, updated_at=now,
                         job_ids=list(job_ids), left_out=list(left_out))
        store.put(BUCKET_COMBINED, combo.id, combo.to_record())
        return combo

    def mutate(record: Record) -> bool:
        record.update(title=clean_title(title), job_ids=list(job_ids), left_out=list(left_out))
        return True

    return _update(store, existing.id, mutate) or existing


def _update(store: Store, combined_id: int, mutate: Callable[[Record], bool]) -> Combined | None:
    applied = [False]

    def wrapped(record: Record) -> None:
        if mutate(record):
            record["updated_at"] = to_iso(utc_now())
            applied[0] = True

    record = store.update_value(BUCKET_COMBINED, combined_id, wrapped)
    return Combined.from_record(record) if record is not None and applied[0] else None


def start_export(store: Store, combined_id: int, kinds: list[str]) -> Combined | None:
    """Refused while any requested export is already running."""

    def mutate(record: Record) -> bool:
        exports = record.setdefault("exports", {})
        if any(exports.get(kind, {}).get("status") == EXPORT_WORKING for kind in kinds):
            return False
        for kind in kinds:
            exports[kind] = {"status": EXPORT_WORKING, "error": None, "url": None, "at": to_iso(utc_now())}
        return True

    return _update(store, combined_id, mutate)


def finish_export(store: Store, combined_id: int, kind: str, *, error: str | None = None, url: str | None = None,
                  file_id: str | None = None) -> None:
    def mutate(record: Record) -> bool:
        if kind == EXPORT_GSLIDES and file_id and not error:
            record["gslides"] = {"file_id": file_id, "url": url}
        record.setdefault("exports", {})[kind] = {
            "status": EXPORT_FAILED if error else EXPORT_DONE,
            "error": str(error)[:ERROR_MAX] if error else None,
            "url": url,
            "at": to_iso(utc_now()),
        }
        return True

    _update(store, combined_id, mutate)


def forget_gslides(store: Store, combined_id: int, file_id: str) -> Combined | None:
    """The deck was trashed in Drive. Refused while a Google export runs or
    once the record points at a different deck."""

    def mutate(record: Record) -> bool:
        exports = record.setdefault("exports", {})
        if exports.get(EXPORT_GSLIDES, {}).get("status") == EXPORT_WORKING:
            return False
        if (record.get("gslides") or {}).get("file_id") != file_id:
            return False
        record["gslides"] = None
        exports.pop(EXPORT_GSLIDES, None)
        return True

    return _update(store, combined_id, mutate)


def fail_interrupted(store: Store) -> int:
    """At server start: exports that were running have no worker any more."""
    count = 0
    for _, record in store.all(BUCKET_COMBINED):
        if not any(state.get("status") == EXPORT_WORKING for state in record.get("exports", {}).values()):
            continue

        def mutate(current: Record) -> bool:
            for state in current.get("exports", {}).values():
                if state.get("status") == EXPORT_WORKING:
                    state["status"] = EXPORT_FAILED
                    state["error"] = "interrupted by a server restart"
            return True

        if _update(store, record["id"], mutate) is not None:
            count += 1
    return count
