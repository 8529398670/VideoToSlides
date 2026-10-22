"""Operations on everything an account can sign in with at once -- its
sessions, its unused login links, and its API keys. They live here, rather
than in users.py, because each one spans several buckets in one transaction
and so needs all three models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.core.db import BUCKET_API_KEYS, BUCKET_USERS, Store
from app.models import to_iso, utc_now
from app.models.api_keys import APIKey
from app.models.login_tokens import delete_pending_login_tokens
from app.models.sessions import destroy_sessions_for_user


@dataclass
class Revoked:
    """What revoke_all_credentials took away."""

    sessions: int = 0
    login_links: int = 0
    api_keys: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def set_user_disabled(store: Store, user_id: int, disabled: bool) -> bool:
    """Disable or re-enable an account; False if there is no such account.

    Disabling also deletes every session the account holds and every login
    link it has not used yet, in the same transaction as the change itself: a
    session or a link that survived would come back to life the moment the
    account was re-enabled. API keys are deliberately left alone -- see
    load_api_key -- and revoke_all_credentials is for when they should die
    too."""
    with store.transaction() as tx:
        record = tx.get(BUCKET_USERS, user_id)
        if record is None:
            return False
        record["disabled_at"] = to_iso(utc_now()) if disabled else None
        tx.put(BUCKET_USERS, user_id, record)
        if disabled:
            destroy_sessions_for_user(tx, user_id)
            delete_pending_login_tokens(tx, user_id)
    return True


def revoke_all_credentials(store: Store, user_id: int) -> Revoked | None:
    """The "this account may be compromised" operation: every session is
    deleted, every unused login link is deleted, and every live API key is
    revoked, in one transaction. Afterwards nothing issued before this moment
    can act as the account; the way back in is a new login link from an
    admin. None if there is no such account.

    It is deliberately separate from the two operations it is easy to mistake
    it for. Issuing a new login link replaces the old link but leaves
    existing sessions alone (someone adding a second device should not be
    signed out of the first), and disabling leaves API keys alone (an account
    disabled for an afternoon should not need every integration re-keyed).
    Neither is a reset."""
    with store.transaction() as tx:
        if tx.get(BUCKET_USERS, user_id) is None:
            return None
        revoked = Revoked(
            sessions=destroy_sessions_for_user(tx, user_id),
            login_links=delete_pending_login_tokens(tx, user_id),
        )
        now = to_iso(utc_now())
        for key_id, record in tx.all(BUCKET_API_KEYS):
            if record["user_id"] == user_id and APIKey.from_record(record).live:
                record["revoked_at"] = now
                tx.put(BUCKET_API_KEYS, key_id, record)
                revoked.api_keys += 1
    return revoked
