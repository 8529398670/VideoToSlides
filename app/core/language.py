"""Every piece of user-facing text, loaded from language.yaml.

The point is that changing what the UI says -- translating it, softening it,
or removing a label entirely -- is a config edit, not a code change. A key
that is absent or empty is a deliberate signal: the frontend hides that
element rather than rendering a blank or a raw key name, which is how you turn
a whole section off without touching the HTML.

It parses bytes rather than opening a path, because which file to read is
app/core/assets.py's decision, not this module's.
"""
from __future__ import annotations

import json
from typing import Any

import yaml


class Language:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values
        # Serialised once at load time -- /api/language is hit on every page
        # load, and re-encoding identical data per request is pure waste.
        self.json = json.dumps(values, ensure_ascii=False, default=str).encode("utf-8")

    def get(self, dotted_key: str) -> str:
        """Resolve a dotted path like "admin.create_button". "" for a missing
        key rather than an error, because "this text is intentionally absent"
        and "this key is misspelled" should both end up showing nothing
        instead of leaking an internal key name into the UI."""
        cursor: Any = self.values
        for part in dotted_key.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return ""
            cursor = cursor[part]
        return cursor if isinstance(cursor, str) else ""


def parse(raw: bytes) -> Language:
    try:
        values = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise RuntimeError(f"could not parse language.yaml: {error}") from None
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise RuntimeError("language.yaml must be a mapping of sections to text")
    return Language(values)
