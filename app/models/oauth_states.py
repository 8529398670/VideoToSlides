"""The short-lived, single-use `state` of a Google sign-in in progress.

Google sends the person back to /api/google/callback with this value. The
record says which user started the flow and holds the PKCE verifier, so the
callback does not depend on the session cookie arriving (it is SameSite=
Strict, and this is a navigation from google.com). It is keyed by a hash of
the state, the same way sessions are, so the database never holds a usable
value; consuming it deletes it.
"""
from __future__ import annotations

from datetime import timedelta

from app.core import encryption
from app.core.db import BUCKET_OAUTH_STATES, Record, Store
from app.models import from_iso, to_iso, utc_now

STATE_TTL = timedelta(minutes=10)


def issue_state(store: Store, user_id: int, verifier: str) -> str:
    state = encryption.url_token(32)
    store.put(BUCKET_OAUTH_STATES, encryption.sha256_hex(state), {
        "user_id": user_id,
        "verifier": verifier,
        "expires_at": to_iso(utc_now() + STATE_TTL),
    })
    return state


def consume_state(store: Store, state: str) -> tuple[int, str] | None:
    """(user_id, verifier) for a live state, deleting it in the same
    transaction so it can never be used twice."""
    key = encryption.sha256_hex(state)
    with store.transaction() as tx:
        record = tx.get(BUCKET_OAUTH_STATES, key)
        if record is None:
            return None
        tx.delete(BUCKET_OAUTH_STATES, key)
    if from_iso(record["expires_at"]) < utc_now():
        return None
    return record["user_id"], record["verifier"]


def purge_expired(store: Store) -> int:
    now = utc_now()

    def match(key: object, record: Record) -> bool:
        return from_iso(record["expires_at"]) < now

    return store.delete_where(BUCKET_OAUTH_STATES, match)
