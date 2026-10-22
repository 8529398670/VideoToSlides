"""Videos on their way up from a browser, in chunks.

The bytes go to <storage>/uploads/<id>.part; this record tracks who is
sending it and how far it has got. A chunk is accepted only at exactly the
offset received so far, which makes a resent chunk harmless and a skipped
one impossible. On completion the file moves into a new job's directory and
this record is deleted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import encryption
from app.core.db import BUCKET_UPLOADS, Record, Store
from app.models import from_iso, to_iso, utc_now

# An upload nobody has added to for this long is abandoned; the purge loop
# deletes the record and its partial file.
UPLOAD_STALE_AFTER = timedelta(hours=24)
FILENAME_MAX = 255


@dataclass
class Upload:
    id: str
    user_id: int
    filename: str
    size: int
    received: int
    created_at: datetime
    updated_at: datetime
    batch: str | None = None

    @property
    def complete(self) -> bool:
        return self.received == self.size

    def to_record(self) -> Record:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "filename": self.filename,
            "size": self.size,
            "received": self.received,
            "batch": self.batch,
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "Upload":
        return cls(
            id=record["id"],
            user_id=record["user_id"],
            filename=record["filename"],
            size=record["size"],
            received=record["received"],
            batch=record.get("batch"),
            created_at=from_iso(record["created_at"]),
            updated_at=from_iso(record["updated_at"]),
        )


def create_upload(store: Store, user_id: int, filename: str, size: int, batch: str | None) -> Upload:
    now = utc_now()
    upload = Upload(
        id=encryption.url_token(18),
        user_id=user_id,
        filename=filename[:FILENAME_MAX],
        size=size,
        received=0,
        batch=batch,
        created_at=now,
        updated_at=now,
    )
    store.put(BUCKET_UPLOADS, upload.id, upload.to_record())
    return upload


def get_upload(store: Store, upload_id: str) -> Upload | None:
    record = store.get(BUCKET_UPLOADS, upload_id)
    return None if record is None else Upload.from_record(record)


def advance(store: Store, upload_id: str, offset: int, length: int) -> int | None:
    """Record that [offset, offset+length) is on disk. Only moves forward,
    and only from exactly where the upload had got to. Returns the new
    received count, or None if the offset was not the expected one."""
    result: list[int | None] = [None]

    def mutate(record: Record) -> None:
        if record["received"] != offset or offset + length > record["size"]:
            return
        record["received"] = offset + length
        record["updated_at"] = to_iso(utc_now())
        result[0] = record["received"]

    store.update_value(BUCKET_UPLOADS, upload_id, mutate)
    return result[0]


def delete_upload(store: Store, upload_id: str) -> None:
    store.delete(BUCKET_UPLOADS, upload_id)


def purge_stale(store: Store) -> list[str]:
    """Delete abandoned uploads; returns their ids so the caller can remove
    the partial files."""
    cutoff = utc_now() - UPLOAD_STALE_AFTER
    doomed: list[str] = []

    def match(key: object, record: Record) -> bool:
        stale = from_iso(record["updated_at"]) < cutoff
        if stale:
            doomed.append(record["id"])
        return stale

    store.delete_where(BUCKET_UPLOADS, match)
    return doomed
