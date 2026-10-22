"""API keys -- the non-browser way in.

They exist so that a script, a cron job, a CI pipeline, or another service can
call the same API a signed-in person calls, without inventing a second
permission model for them.

A key carries a role from the same set users do -- admin or user -- so
whatever an admin may do in the UI an admin-scoped key may do over HTTP, and a
user-scoped key is limited to exactly what a non-admin account is. There is no
separate scope language to keep in step with the role checks.

A key can never outrank its owner. The role stored on a key is a *request*,
and it is bounded twice: issue_api_key refuses to mint an admin key for a
non-admin account, and narrow_role re-applies the ceiling on every request.
The second check is what covers an account that is demoted after the key was
minted: the stored role still says admin, and the effective role does not.

Same "<id>.<secret>" shape as sessions, and the same hash. The id is the
record key, so authenticating a key is one fetch and one hash comparison.
sha256 rather than bcrypt because this runs on every request against a
256-bit random secret, exactly like a session; bcrypt here would be a latency
tax that buys nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import encryption
from app.core.db import BUCKET_API_KEYS, BUCKET_USERS, Record, Store
from app.models import from_iso, to_iso, utc_now
from app.models.sessions import split_credential
from app.models.users import ROLE_ADMIN, ROLE_USER, User, get_user, valid_role

# How many live keys one account can hold. Any signed-in user can mint keys,
# so without a cap a single account can grow the bucket without limit. 25 is
# far past what a person needs and low enough to stay a bound; revoking one
# frees a slot.
API_KEY_MAX_PER_USER = 25

# How many rows one account keeps, dead keys included. The live cap alone
# does not bound anything: mint, revoke, repeat, and the retained history
# grows by one row each time, and every listing and quota check decrypts all
# of it. When an account reaches this, issuing a key drops its oldest dead
# rows to make room.
API_KEY_MAX_ROWS_PER_USER = 100

# How stale last_used_at is allowed to get. "When was this key last used" is
# the question an operator asks before revoking something, so it is worth
# recording -- but a write per authenticated request would put every handler
# that needs to write behind it. At most once per interval keeps the answer
# useful (to within 15 minutes) and the cost negligible.
API_KEY_TOUCH_INTERVAL = timedelta(minutes=15)

# How long a dead key's row is kept before the purge drops it. A revoked key
# that vanished immediately would make the listing lie about what existed last
# week; keeping it forever is the other wrong trade.
API_KEY_RETENTION = timedelta(days=30)

# Bounds an explicit expiry. Zero still means "never expires", asked for
# deliberately; this only stops a typo from producing a date so far out that
# it is the same thing by accident.
API_KEY_MAX_DAYS = 3650


class APIKeyError(Exception):
    """A key that cannot be issued. The message is safe to show."""


class APIKeyLimitError(APIKeyError):
    def __init__(self) -> None:
        super().__init__("this account already holds the maximum number of live API keys")


class APIKeyRoleError(APIKeyError):
    def __init__(self) -> None:
        super().__init__("an API key cannot be given a role its owner does not have")


class APIKeyDisabledError(APIKeyError):
    def __init__(self) -> None:
        super().__init__("cannot issue an API key for a disabled account")


class APIKeyOwnerError(APIKeyError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"no user with id {user_id}")


@dataclass
class APIKey:
    id: str
    user_id: int
    name: str
    role: str
    secret_hash: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and utc_now() > self.expires_at

    @property
    def live(self) -> bool:
        """Whether the key would authenticate right now, as far as the key
        itself is concerned. Whether its owner is still enabled is a separate
        question, asked in load_api_key."""
        return not self.revoked and not self.expired

    def to_record(self) -> Record:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "secret_hash": self.secret_hash,
            "created_at": to_iso(self.created_at),
            "expires_at": to_iso(self.expires_at),
            "last_used_at": to_iso(self.last_used_at),
            "revoked_at": to_iso(self.revoked_at),
        }

    @classmethod
    def from_record(cls, record: Record) -> "APIKey":
        return cls(
            id=record["id"],
            user_id=record["user_id"],
            name=record["name"],
            role=record["role"],
            secret_hash=record["secret_hash"],
            created_at=from_iso(record["created_at"]),
            expires_at=from_iso(record.get("expires_at")),
            last_used_at=from_iso(record.get("last_used_at")),
            revoked_at=from_iso(record.get("revoked_at")),
        )


def ttl_from_days(days: int | None, fallback_seconds: int) -> int | None:
    """Turn a requested lifetime in days into seconds. It lives here rather
    than in a handler so the HTTP API and the CLI cannot end up disagreeing
    about what "0 days" means.

    None means "use the configured default", 0 means "never expires" (returned
    as 0), and a number means that many days. Returns None for a value that is
    out of range."""
    if days is None:
        return fallback_seconds
    if isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > API_KEY_MAX_DAYS:
        return None
    return days * 24 * 60 * 60


def valid_name(name: str) -> bool:
    """Bounds the label a key is given. It is free text a user controls, same
    as a display name, and exists only so a list of keys is readable --
    "deploy bot", "grafana", "my laptop"."""
    return isinstance(name, str) and 1 <= len(name.strip()) <= 80


def narrow_role(account_role: str, key_role: str) -> str:
    """The permission ceiling, applied on every request that authenticates
    with a key: the effective role is the *lower* of the account's role and
    the key's.

    Storing the key's role and then re-deriving the effective one, rather than
    trusting what was stored, is what makes demotion work. If an admin mints
    an admin key and later becomes an ordinary user, the row still says admin
    -- and every request it authenticates is treated as a user."""
    if account_role == ROLE_ADMIN and key_role == ROLE_ADMIN:
        return ROLE_ADMIN
    return ROLE_USER


def issue_api_key(store: Store, user_id: int, name: str, role: str, ttl_seconds: int) -> tuple[str, APIKey]:
    """Returns (credential, key). The credential exists exactly once, here: it
    is never stored and cannot be recovered -- if it is lost, revoke the key
    and mint another.

    ttl_seconds of zero means the key never expires. That is a deliberate
    option rather than an oversight: a CI job nobody will remember to re-key is
    worse served by a credential that dies silently at 3am. It has to be asked
    for explicitly; the default comes from configuration."""
    # The owner is loaded inside the transaction below rather than taking the
    # caller's word for the role. Every route into this function -- the HTTP
    # API and the CLI -- then gets the same ceiling without each having to
    # remember it.
    if not valid_role(role):
        role = ROLE_USER

    key_id = encryption.url_token(12)
    secret = encryption.url_token(32)
    now = utc_now()
    key = APIKey(
        id=key_id,
        user_id=user_id,
        name=name.strip(),
        role=role,
        secret_hash=encryption.sha256_hex(secret),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds > 0 else None,
    )

    # The owner check, the quota check, and the insert share one transaction.
    # Counted in one transaction and inserted in another, sixteen concurrent
    # requests can all see 24 live keys and all insert, which is how a "max
    # 25" turns into 40.
    with store.transaction() as tx:
        owner_record = tx.get(BUCKET_USERS, user_id)
        if owner_record is None:
            raise APIKeyOwnerError(user_id)
        owner = User.from_record(owner_record)
        if owner.disabled:
            raise APIKeyDisabledError()
        if role == ROLE_ADMIN and not owner.is_admin:
            raise APIKeyRoleError()

        owned = [APIKey.from_record(record) for _, record in tx.all(BUCKET_API_KEYS) if record["user_id"] == user_id]
        if sum(1 for existing in owned if existing.live) >= API_KEY_MAX_PER_USER:
            raise APIKeyLimitError()
        # Oldest dead rows first. Live keys are capped well below the row
        # limit, so there is always a dead row to drop when the limit is hit.
        dead = sorted((existing for existing in owned if not existing.live), key=lambda existing: existing.created_at)
        for existing in dead[: max(0, len(owned) + 1 - API_KEY_MAX_ROWS_PER_USER)]:
            tx.delete(BUCKET_API_KEYS, existing.id)

        tx.put(BUCKET_API_KEYS, key_id, key.to_record())
    return f"{key_id}.{secret}", key


def load_api_key(store: Store, credential: str) -> tuple[APIKey, User] | None:
    """None for anything that is not a usable key -- wrong shape, unknown id,
    bad secret, expired, revoked, or belonging to a disabled account. Callers
    only ever need "is this request authenticated", and folding every failure
    into one answer avoids leaking which check failed.

    Note what is *not* here: a disabled account does not have its keys
    revoked, the way it has its sessions destroyed. A session is a browser
    artifact and destroying it costs nothing; a key is a configured
    integration, and tearing every integration's credential down because
    someone was disabled for an afternoon is a footgun with no security gain
    -- the check below already refuses the request either way."""
    parts = split_credential(credential)
    if parts is None:
        return None
    key_id, secret = parts

    record = store.get(BUCKET_API_KEYS, key_id)
    if record is None:
        return None
    key = APIKey.from_record(record)

    if not encryption.constant_time_equal(key.secret_hash, encryption.sha256_hex(secret)):
        return None
    if not key.live:
        return None

    user = get_user(store, key.user_id)
    if user is None or user.disabled:
        return None

    _touch(store, key)
    return key, user


def _touch(store: Store, key: APIKey) -> None:
    """Record use at most once per API_KEY_TOUCH_INTERVAL. Failures are
    ignored on purpose: this is bookkeeping, and a request that authenticated
    correctly should not fail because a timestamp could not be written."""
    now = utc_now()
    if key.last_used_at is not None and now - key.last_used_at < API_KEY_TOUCH_INTERVAL:
        return

    def mutate(record: Record) -> None:
        record["last_used_at"] = to_iso(now)

    try:
        store.update_value(BUCKET_API_KEYS, key.id, mutate)
    except Exception:  # noqa: BLE001 -- bookkeeping only, see above
        return
    key.last_used_at = now


def get_api_key(store: Store, key_id: str) -> APIKey | None:
    record = store.get(BUCKET_API_KEYS, key_id)
    return None if record is None else APIKey.from_record(record)


def list_api_keys(store: Store) -> list[APIKey]:
    """Every key, oldest first. Only the admin endpoint and the CLI use it; a
    user asking for "my keys" goes through list_api_keys_for_user."""
    keys = [APIKey.from_record(record) for _, record in store.all(BUCKET_API_KEYS)]
    return sorted(keys, key=lambda key: key.created_at)


def list_api_keys_for_user(store: Store, user_id: int) -> list[APIKey]:
    return [key for key in list_api_keys(store) if key.user_id == user_id]


def count_live_api_keys_for_user(store: Store, user_id: int) -> int:
    return sum(1 for key in list_api_keys_for_user(store, user_id) if key.live)


def revoke_api_key(store: Store, key_id: str) -> APIKey | None:
    """Mark a key dead and return it, or None if there is no such key. It is
    idempotent -- revoking twice keeps the first timestamp -- and goes through
    update_value so two concurrent revocations cannot have one overwrite the
    other with a stale copy of the row.

    The row is marked rather than deleted so a listing can still show the key
    existed and when it died. The purge removes it later."""

    def mutate(record: Record) -> None:
        if record.get("revoked_at") is None:
            record["revoked_at"] = to_iso(utc_now())

    record = store.update_value(BUCKET_API_KEYS, key_id, mutate)
    return None if record is None else APIKey.from_record(record)


def purge_dead_api_keys(store: Store) -> int:
    """Drop rows for keys revoked or expired longer than API_KEY_RETENTION
    ago. Dead keys are already refused by load_api_key, so this is only
    housekeeping."""
    cutoff = utc_now() - API_KEY_RETENTION

    def dead(_key: object, record: Record) -> bool:
        revoked_at = from_iso(record.get("revoked_at"))
        expires_at = from_iso(record.get("expires_at"))
        return (revoked_at is not None and revoked_at < cutoff) or (expires_at is not None and expires_at < cutoff)

    return store.delete_where(BUCKET_API_KEYS, dead)
