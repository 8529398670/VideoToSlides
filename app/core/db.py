"""The database: one SQLite file, used as a store of records.

Every model goes through this module and nothing else touches SQLite. Each
record is a JSON document, and with server.encrypt_at_rest on (the default)
it is XChaCha-encrypted with server.secret_key before it is written. Doing
that here, in encode/decode, is what makes at-rest encryption a single switch
instead of something each model has to remember -- and it means the answer to
"where does data get written" is one file.

The shape mirrors a key/value store on purpose: a "bucket" is a table of
(key, value) rows. Encrypted values cannot be queried with SQL, so models look
records up by key and scan a bucket for anything else -- the same trade the
Go template makes with bolt, and fine at the size of app this template is for.
A feature that genuinely needs SQL over its data can add its own table through
connect(); its columns are then not encrypted at rest, and that is a decision
to make deliberately (see references/architecture.md).

SQLite runs in WAL mode, so the server and a `manage` command can both have
the file open: readers never block, and a writer waits (up to the busy
timeout) for another writer rather than failing.

Everything here is synchronous. Call it from a thread, never directly from an
async handler -- see run_in_threadpool in app/routes/helpers.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.core import encryption

# Bucket names. Add new ones here and to BUCKETS so their tables are created
# at startup -- a table that only gets created on first write is an
# "no such table" error waiting for the first reader.
BUCKET_USERS = "users"
BUCKET_SESSIONS = "sessions"
BUCKET_LOGIN_TOKENS = "login_tokens"
BUCKET_API_KEYS = "api_keys"
BUCKET_META = "meta"
BUCKET_JOBS = "jobs"
BUCKET_UPLOADS = "uploads"
BUCKET_GOOGLE_ACCOUNTS = "google_accounts"
BUCKET_OAUTH_STATES = "oauth_states"
BUCKET_SETTINGS = "settings"
BUCKET_COMBINED = "combined"

BUCKETS = (
    BUCKET_USERS,
    BUCKET_SESSIONS,
    BUCKET_LOGIN_TOKENS,
    BUCKET_API_KEYS,
    BUCKET_META,
    BUCKET_JOBS,
    BUCKET_UPLOADS,
    BUCKET_GOOGLE_ACCOUNTS,
    BUCKET_OAUTH_STATES,
    BUCKET_SETTINGS,
    BUCKET_COMBINED,
)

# How long a writer waits for another writer before giving up. Without it a
# second process (a `manage` command during a busy moment) fails at once with
# "database is locked" instead of simply waiting its turn.
BUSY_TIMEOUT_SECONDS = 5.0

Record = dict[str, Any]
Key = int | str


class DecodeError(Exception):
    """A stored record that cannot be read back -- almost always a
    server.secret_key that is not the one the record was written with."""


def _table(bucket: str) -> str:
    # Table names cannot be bound as parameters, so only the names declared
    # above are ever interpolated into SQL.
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r} -- add it to BUCKETS in app/core/db.py")
    return f'"{bucket}"'


class Store:
    def __init__(self, path: Path, secret_key: str, encrypt_at_rest: bool) -> None:
        self.path = path
        self._secret_key = secret_key
        self._encrypt_at_rest = encrypt_at_rest

    # -- connections -------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """A fresh connection in autocommit mode; transactions are opened
        explicitly where they are needed. One connection per operation keeps
        this safe to call from any thread."""
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction taken up front (BEGIN IMMEDIATE). Taking the
        write lock before the first read is what closes the lost-update gap:
        two callers cannot both read the old row and then both write."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def initialise(self) -> None:
        with self._connection() as connection:
            # WAL is a property of the file, so setting it once is enough;
            # doing it on every start is harmless and keeps it true.
            connection.execute("PRAGMA journal_mode = WAL")
            for bucket in BUCKETS:
                # The key column is deliberately untyped: integer keys (user
                # ids) stay integers and sort numerically, string keys
                # (credential ids) stay strings.
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {_table(bucket)} (key PRIMARY KEY NOT NULL, value BLOB NOT NULL)"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sequences (bucket TEXT PRIMARY KEY NOT NULL, value INTEGER NOT NULL)"
            )

    # -- encoding ----------------------------------------------------------

    def encode(self, record: Record) -> bytes:
        raw = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if not self._encrypt_at_rest:
            return raw
        return encryption.xchacha_encrypt_bytes(self._secret_key, raw)

    def decode(self, raw: bytes) -> Record:
        plain = raw
        if self._encrypt_at_rest:
            try:
                plain = encryption.xchacha_decrypt_bytes(self._secret_key, bytes(raw))
            except encryption.CipherTextError:
                raise DecodeError(
                    "could not decrypt a stored record -- is server.secret_key in config.yaml "
                    "the same one it was written with?"
                ) from None
        try:
            return json.loads(plain)
        except (ValueError, UnicodeDecodeError):
            raise DecodeError(
                "a stored record is not valid JSON -- was server.encrypt_at_rest changed after data was written?"
            ) from None

    # -- the record API ----------------------------------------------------

    def put(self, bucket: str, key: Key, record: Record) -> None:
        encoded = self.encode(record)
        with self._connection() as connection:
            connection.execute(f"INSERT OR REPLACE INTO {_table(bucket)} (key, value) VALUES (?, ?)", (key, encoded))

    def get(self, bucket: str, key: Key) -> Record | None:
        """None means "no such record", which callers need to tell apart from
        a record that exists."""
        with self._connection() as connection:
            row = connection.execute(f"SELECT value FROM {_table(bucket)} WHERE key = ?", (key,)).fetchone()
        return None if row is None else self.decode(row[0])

    def delete(self, bucket: str, key: Key) -> None:
        with self._connection() as connection:
            connection.execute(f"DELETE FROM {_table(bucket)} WHERE key = ?", (key,))

    def all(self, bucket: str) -> list[tuple[Key, Record]]:
        """Every record in a bucket, in key order."""
        with self._connection() as connection:
            rows = connection.execute(f"SELECT key, value FROM {_table(bucket)} ORDER BY key").fetchall()
        return [(key, self.decode(value)) for key, value in rows]

    def next_sequence(self, bucket: str) -> int:
        """Monotonic ids handed out inside a write transaction, so two
        concurrent creates can never collide on the same id."""
        _table(bucket)
        with self._write() as connection:
            connection.execute(
                "INSERT INTO sequences (bucket, value) VALUES (?, 1) "
                "ON CONFLICT (bucket) DO UPDATE SET value = value + 1",
                (bucket,),
            )
            return connection.execute("SELECT value FROM sequences WHERE bucket = ?", (bucket,)).fetchone()[0]

    def update_value(self, bucket: str, key: Key, mutate: Callable[[Record], None]) -> Record | None:
        """Read a record, hand it to mutate (which changes it in place), and
        write the result back inside one write transaction. Returns the
        updated record, or None if there was no such record.

        The obvious alternative -- get, change, put -- has a lost update
        hiding in the gap between the two: two concurrent callers both read
        the old record and the second put silently discards the first one's
        change. Anything that edits an existing record should come through
        here."""
        table = _table(bucket)
        with self._write() as connection:
            row = connection.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            record = self.decode(row[0])
            mutate(record)
            connection.execute(f"UPDATE {table} SET value = ? WHERE key = ?", (self.encode(record), key))
            return record

    def delete_where(self, bucket: str, match: Callable[[Key, Record], bool]) -> int:
        """Remove every record match() accepts, inside one write transaction.

        Encrypted values cannot be filtered in SQL, so this decodes each row.
        Doing the scan and the deletes in one transaction matters: a record
        that starts matching between a separate read pass and the deletes
        would be missed, which for session revocation means a session created
        mid-sweep outlives the account it belongs to."""
        table = _table(bucket)
        with self._write() as connection:
            rows = connection.execute(f"SELECT key, value FROM {table}").fetchall()
            doomed = [(key,) for key, value in rows if match(key, self.decode(value))]
            connection.executemany(f"DELETE FROM {table} WHERE key = ?", doomed)
            return len(doomed)

    @contextmanager
    def transaction(self) -> Iterator["Transaction"]:
        """A write transaction with the same record API, for the operations
        that have to be atomic across several records or buckets: turning a
        login link into a session, disabling an account together with every
        credential it holds, checking an API key quota and inserting the key.

        It takes the write lock up front (BEGIN IMMEDIATE), so no other write
        interleaves with it. That is the point -- and the cost: keep slow work
        (bcrypt, network calls) outside, because every other writer waits."""
        with self._write() as connection:
            yield Transaction(self, connection)


class Transaction:
    def __init__(self, store: Store, connection: sqlite3.Connection) -> None:
        self._store = store
        self._connection = connection

    def get(self, bucket: str, key: Key) -> Record | None:
        row = self._connection.execute(f"SELECT value FROM {_table(bucket)} WHERE key = ?", (key,)).fetchone()
        return None if row is None else self._store.decode(row[0])

    def put(self, bucket: str, key: Key, record: Record) -> None:
        self._connection.execute(
            f"INSERT OR REPLACE INTO {_table(bucket)} (key, value) VALUES (?, ?)", (key, self._store.encode(record))
        )

    def delete(self, bucket: str, key: Key) -> None:
        self._connection.execute(f"DELETE FROM {_table(bucket)} WHERE key = ?", (key,))

    def all(self, bucket: str) -> list[tuple[Key, Record]]:
        rows = self._connection.execute(f"SELECT key, value FROM {_table(bucket)} ORDER BY key").fetchall()
        return [(key, self._store.decode(value)) for key, value in rows]

    def delete_where(self, bucket: str, match: Callable[[Key, Record], bool]) -> int:
        doomed = [(key,) for key, record in self.all(bucket) if match(key, record)]
        self._connection.executemany(f"DELETE FROM {_table(bucket)} WHERE key = ?", doomed)
        return len(doomed)


def open_store(database_path: Path, secret_key: str, encrypt_at_rest: bool) -> Store:
    """Create the file if needed and make sure every bucket's table exists."""
    store = Store(database_path, secret_key, encrypt_at_rest)
    try:
        # Created 0600 up front: SQLite would otherwise use the umask, and it
        # gives the -wal and -shm files the database file's own mode.
        if not database_path.exists():
            os.close(os.open(database_path, os.O_WRONLY | os.O_CREAT, 0o600))
        store.initialise()
    except OSError as error:
        raise RuntimeError(f"could not create the database at {database_path}: {error.strerror}") from None
    except sqlite3.Error as error:
        raise RuntimeError(f"could not open the database at {database_path}: {error}") from None
    return store
