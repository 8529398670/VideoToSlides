"""What an admin changed in Settings: one record of {setting key: value}.
Defaults and validation live in app/core/settings.py; this only stores the
overrides. Encrypted at rest like every record -- it can hold the Google
client secret."""
from __future__ import annotations

from typing import Any, Callable

from app.core.db import BUCKET_SETTINGS, Store

KEY = "overrides"


def get_overrides(store: Store) -> dict[str, Any]:
    record = store.get(BUCKET_SETTINGS, KEY)
    return dict(record.get("values", {})) if record else {}


def update_overrides(store: Store, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Read, change, and write in one transaction, so two admins saving at
    once cannot drop each other's edits. `change` may raise to abort."""
    with store.transaction() as tx:
        record = tx.get(BUCKET_SETTINGS, KEY) or {"values": {}}
        values = change(dict(record.get("values", {})))
        tx.put(BUCKET_SETTINGS, KEY, {"values": values})
        return values
