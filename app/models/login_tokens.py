"""One-time login tokens -- this project's entire replacement for a signup
form and a password reset flow. An admin (or the first-run bootstrap) mints
one for a specific user; redeeming the link is the login.

Same "<id>.<secret>" shape as a session, but the secret is bcrypt-hashed
rather than sha256'd. bcrypt is affordable here precisely because it runs once
per redemption instead of once per request, and the id prefix is what keeps
that to a single hash comparison -- hashing every candidate row would hand
anyone a trivial way to pin the CPU.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import encryption
from app.core.db import BUCKET_LOGIN_TOKENS, BUCKET_SESSIONS, BUCKET_USERS, Record, Store, Transaction
from app.models import from_iso, to_iso, utc_now
from app.models.sessions import new_session, split_credential
from app.models.users import User


@dataclass
class LoginToken:
    user_id: int
    secret_hash: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    def usable(self) -> bool:
        return self.used_at is None and utc_now() <= self.expires_at

    def to_record(self) -> Record:
        return {
            "user_id": self.user_id,
            "secret_hash": self.secret_hash,
            "created_at": to_iso(self.created_at),
            "expires_at": to_iso(self.expires_at),
            "used_at": to_iso(self.used_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "LoginToken":
        return cls(
            user_id=record["user_id"],
            secret_hash=record["secret_hash"],
            created_at=from_iso(record["created_at"]),
            expires_at=from_iso(record["expires_at"]),
            used_at=from_iso(record.get("used_at")),
        )


class UnknownUserError(Exception):
    """issue_login_token was given a user id with no account behind it."""


def issue_login_token(store: Store, user_id: int, ttl_seconds: int) -> str:
    """Returns the raw credential exactly once. It is never stored and cannot
    be recovered -- if it is lost, mint another one.

    An account has at most one pending link. Issuing a new one deletes any
    unused link the account already had, in the same transaction, so "send
    them a new link" also kills the old one -- which is what you want when the
    old one went to the wrong place, and costs nothing when it simply
    expired."""
    token_id = encryption.url_token(12)
    secret = encryption.url_token(24)
    now = utc_now()
    token = LoginToken(
        user_id=user_id,
        # bcrypt outside the transaction, so the write lock is not held for it.
        secret_hash=encryption.bcrypt_hash(secret),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    with store.transaction() as tx:
        if tx.get(BUCKET_USERS, user_id) is None:
            raise UnknownUserError(user_id)
        delete_pending_login_tokens(tx, user_id)
        tx.put(BUCKET_LOGIN_TOKENS, token_id, token.to_record())
    return f"{token_id}.{secret}"


def redeem_login_token(store: Store, credential: str, session_ttl_seconds: int) -> tuple[str, int] | None:
    """Consume a login link and create the session it grants. Returns
    (session credential, user id), or None for any failure -- expired, spent,
    never existed, the wrong secret, or a disabled account all look the same,
    so the answer cannot be used to probe which links are real.

    The bcrypt comparison runs before the write transaction, so the database's
    single write lock is never held for the deliberately slow part. Everything
    that decides the outcome is then re-checked inside one transaction: the
    token is still unused and unexpired, it is the same token that was
    compared, and the account exists and is enabled. Marking the token used
    and storing the session happen there too. So two requests racing the same
    link cannot both get a session, and a link redeemed while the account is
    being disabled either lands first (and set_user_disabled deletes its
    session) or lands second (and is refused)."""
    parts = split_credential(credential)
    if parts is None:
        return None
    token_id, secret = parts

    record = store.get(BUCKET_LOGIN_TOKENS, token_id)
    if record is None:
        return None
    token = LoginToken.from_record(record)
    if not token.usable() or not encryption.bcrypt_compare(token.secret_hash, secret):
        return None

    with store.transaction() as tx:
        record = tx.get(BUCKET_LOGIN_TOKENS, token_id)
        if record is None:
            return None
        current = LoginToken.from_record(record)
        if not current.usable() or not encryption.constant_time_equal(current.secret_hash, token.secret_hash):
            return None
        owner = tx.get(BUCKET_USERS, current.user_id)
        if owner is None or User.from_record(owner).disabled:
            return None

        current.used_at = utc_now()
        tx.put(BUCKET_LOGIN_TOKENS, token_id, current.to_record())
        session_id, session_credential, session = new_session(current.user_id, session_ttl_seconds)
        tx.put(BUCKET_SESSIONS, session_id, session.to_record())
        return session_credential, current.user_id


def delete_pending_login_tokens(tx: Transaction, user_id: int) -> int:
    """Remove every unused link an account holds, inside the caller's
    transaction. Used ones are left for the purge, so a second click on a
    spent link still fails as "already used"."""
    return tx.delete_where(
        BUCKET_LOGIN_TOKENS,
        lambda _key, record: record["user_id"] == user_id and record.get("used_at") is None,
    )


def purge_expired_login_tokens(store: Store) -> int:
    """Drop spent and stale rows. Used tokens are kept until they expire
    rather than deleted on redemption, so a second click on a link fails as
    "already used" instead of looking like it never existed."""
    now = utc_now()
    return store.delete_where(BUCKET_LOGIN_TOKENS, lambda _key, record: now > from_iso(record["expires_at"]))
