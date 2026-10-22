"""A user's connection to their own Google account, for Slides export.

Holds the OAuth refresh token, which can create files in that person's Drive
(the drive.file scope: only files this app made). It is encrypted at rest
like every record, never sent to the browser, and revoked at Google when the
user disconnects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.db import BUCKET_GOOGLE_ACCOUNTS, Record, Store
from app.models import from_iso, to_iso, utc_now


@dataclass
class GoogleAccount:
    user_id: int
    refresh_token: str
    email: str
    connected_at: datetime

    def to_record(self) -> Record:
        return {
            "user_id": self.user_id,
            "refresh_token": self.refresh_token,
            "email": self.email,
            "connected_at": to_iso(self.connected_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "GoogleAccount":
        return cls(
            user_id=record["user_id"],
            refresh_token=record["refresh_token"],
            email=record.get("email", ""),
            connected_at=from_iso(record["connected_at"]),
        )


def save_account(store: Store, user_id: int, refresh_token: str, email: str) -> GoogleAccount:
    account = GoogleAccount(user_id=user_id, refresh_token=refresh_token, email=email, connected_at=utc_now())
    store.put(BUCKET_GOOGLE_ACCOUNTS, user_id, account.to_record())
    return account


def get_account(store: Store, user_id: int) -> GoogleAccount | None:
    record = store.get(BUCKET_GOOGLE_ACCOUNTS, user_id)
    return None if record is None else GoogleAccount.from_record(record)


def delete_account(store: Store, user_id: int) -> None:
    store.delete(BUCKET_GOOGLE_ACCOUNTS, user_id)
