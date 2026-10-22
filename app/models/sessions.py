"""The server side of a login.

The cookie never carries the user id or the role -- only an opaque reference
-- so revoking access is a matter of deleting this record, with no token
denylist to maintain.

The credential is shaped "<id>.<secret>": the id is the record key, so lookup
is a single fetch, and only the secret's hash is stored. A stolen database
therefore contains nothing that can be replayed as a cookie.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import encryption
from app.core.db import BUCKET_SESSIONS, Record, Store, Transaction
from app.models import from_iso, to_iso, utc_now
from app.models.users import User, get_user


@dataclass
class Session:
    user_id: int
    secret_hash: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime

    def to_record(self) -> Record:
        return {
            "user_id": self.user_id,
            "secret_hash": self.secret_hash,
            "csrf_token": self.csrf_token,
            "created_at": to_iso(self.created_at),
            "expires_at": to_iso(self.expires_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "Session":
        return cls(
            user_id=record["user_id"],
            secret_hash=record["secret_hash"],
            csrf_token=record["csrf_token"],
            created_at=from_iso(record["created_at"]),
            expires_at=from_iso(record["expires_at"]),
        )


def split_credential(raw: str) -> tuple[str, str] | None:
    """Split "<id>.<secret>" into (id, secret), or None for anything else.
    Shared by sessions, login tokens, and API keys, which all use the shape."""
    if not isinstance(raw, str):
        return None
    record_id, dot, secret = raw.partition(".")
    if not dot or not record_id or not secret:
        return None
    return record_id, secret


def new_session(user_id: int, ttl_seconds: int) -> tuple[str, str, Session]:
    """Returns (session_id, credential, session) without storing anything.
    The only caller is redeem_login_token, which stores the session in the
    same transaction that consumes the login link and checks the account is
    enabled -- so there is deliberately no create_session that writes one on
    its own. The credential is set on the cookie and never stored; only its
    secret's hash is."""
    session_id = encryption.url_token(16)
    secret = encryption.url_token(32)
    now = utc_now()
    session = Session(
        user_id=user_id,
        secret_hash=encryption.sha256_hex(secret),
        csrf_token=encryption.url_token(24),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return session_id, f"{session_id}.{secret}", session


def load_session(store: Store, credential: str) -> tuple[Session, User] | None:
    """None for anything that is not a live session -- wrong shape, unknown
    id, bad secret, expired, or belonging to a disabled user. Callers only
    ever ask "am I logged in", and folding every failure into the same answer
    avoids leaking which check failed."""
    parts = split_credential(credential)
    if parts is None:
        return None
    session_id, secret = parts

    record = store.get(BUCKET_SESSIONS, session_id)
    if record is None:
        return None
    session = Session.from_record(record)

    if not encryption.constant_time_equal(session.secret_hash, encryption.sha256_hex(secret)):
        return None
    if utc_now() > session.expires_at:
        store.delete(BUCKET_SESSIONS, session_id)
        return None

    user = get_user(store, session.user_id)
    if user is None or user.disabled:
        return None
    return session, user


def destroy_session(store: Store, credential: str) -> None:
    parts = split_credential(credential)
    if parts is not None:
        store.delete(BUCKET_SESSIONS, parts[0])


def destroy_sessions_for_user(tx: Transaction, user_id: int) -> int:
    """Delete every session an account holds, inside the caller's
    transaction. Deleting sessions is only ever half of a revocation, so this
    takes a transaction rather than opening one: set_user_disabled and
    revoke_all_credentials (app/models/credentials.py) run it together with
    the account change, and a login link redeemed at the same moment either
    lands before (and its session is deleted here) or after (and sees the
    change)."""
    return tx.delete_where(BUCKET_SESSIONS, lambda _key, record: record["user_id"] == user_id)


def purge_expired_sessions(store: Store) -> int:
    """Called hourly from the server's lifespan task. Expired rows are already
    refused by load_session, so this is only housekeeping to stop the file
    growing without bound -- one transaction, for the same reason as above."""
    now = utc_now()
    return store.delete_where(BUCKET_SESSIONS, lambda _key, record: now > from_iso(record["expires_at"]))
