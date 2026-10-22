"""One module per stored record type. There is no ORM and no query builder --
each module is a small set of plain functions over app.core.db.Store, which is
enough for a store of records and keeps the storage layer readable end to end.

Records are JSON, so timestamps are stored as ISO 8601 strings in UTC; the
helpers below are the only place that conversion happens.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def from_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
