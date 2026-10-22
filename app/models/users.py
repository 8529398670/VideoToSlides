"""User accounts.

There is no password field, and adding one would undo the entire design:
identity is proven only by redeeming a login link, presenting a session
cookie, or presenting an API key. See references/architecture.md before
changing that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.db import BUCKET_USERS, Record, Store
from app.models import from_iso, to_iso, utc_now

ROLE_ADMIN = "admin"
ROLE_USER = "user"


@dataclass
class User:
    id: int
    display_name: str
    role: str
    created_at: datetime
    disabled_at: datetime | None = None

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_record(self) -> Record:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "role": self.role,
            "created_at": to_iso(self.created_at),
            "disabled_at": to_iso(self.disabled_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "User":
        return cls(
            id=record["id"],
            display_name=record["display_name"],
            role=record["role"],
            created_at=from_iso(record["created_at"]),
            disabled_at=from_iso(record.get("disabled_at")),
        )


def valid_role(role: str) -> bool:
    return role in (ROLE_ADMIN, ROLE_USER)


def valid_display_name(display_name: str) -> bool:
    """Bounds the one piece of free text a user controls. The cap is about
    keeping the database and the UI sane; escaping for display is the
    frontend's job (static/js/dom.js never uses innerHTML for user values)."""
    return isinstance(display_name, str) and 1 <= len(display_name.strip()) <= 80


def create_user(store: Store, display_name: str, role: str) -> User:
    if not valid_role(role):
        raise ValueError(f"invalid role: {role!r}")
    user = User(
        id=store.next_sequence(BUCKET_USERS),
        display_name=display_name.strip(),
        role=role,
        created_at=utc_now(),
    )
    store.put(BUCKET_USERS, user.id, user.to_record())
    return user


def get_user(store: Store, user_id: int) -> User | None:
    record = store.get(BUCKET_USERS, user_id)
    return None if record is None else User.from_record(record)


def list_users(store: Store) -> list[User]:
    return [User.from_record(record) for _, record in store.all(BUCKET_USERS)]


def rename_user(store: Store, user_id: int, display_name: str) -> bool:
    """Goes through update_value rather than get_user followed by a put. The
    read and the write land in one transaction, which is what stops two
    concurrent admin requests from each reading the old row and the slower one
    silently discarding the other's change.

    Disabling an account is not here: it has to take the account's sessions
    and login links with it in the same transaction, so it lives in
    app/models/credentials.py beside the other operations that span
    buckets."""

    def mutate(record: Record) -> None:
        record["display_name"] = display_name.strip()

    return store.update_value(BUCKET_USERS, user_id, mutate) is not None


def any_admin_exists(store: Store) -> bool:
    """Decides whether the server prints a first-run login link on boot. Once
    one enabled admin exists, that bootstrap path stays closed."""
    return any(user.is_admin and not user.disabled for user in list_users(store))
