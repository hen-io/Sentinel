"""Single shared SQLite connection; all calls run in the executor, serialized by a lock."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        live_id TEXT,
        channel TEXT NOT NULL,
        created TEXT NOT NULL,
        acknowledged INTEGER NOT NULL,
        record_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sentinel_created ON notifications(created)",
    "CREATE INDEX IF NOT EXISTS idx_sentinel_live_id ON notifications(live_id)",
    "CREATE INDEX IF NOT EXISTS idx_sentinel_channel ON notifications(channel)",
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        chatroom_id TEXT NOT NULL,
        sender_user_id TEXT,
        created TEXT NOT NULL,
        message_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sentinel_chat_chatroom_created ON chat_messages(chatroom_id, created)",
    """
    CREATE TABLE IF NOT EXISTS chat_reads (
        chatroom_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        last_read_message_id TEXT,
        last_read_at TEXT NOT NULL,
        PRIMARY KEY (chatroom_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_reactions (
        message_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        emoji TEXT NOT NULL,
        created TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id, emoji)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sentinel_chat_reactions_message ON chat_reactions(message_id)",
    # Leftovers from earlier versions.
    "DROP TABLE IF EXISTS meta",
    *(
        f"DROP INDEX IF EXISTS idx_k93_{name}"
        for name in ("created", "live_id", "channel", "chat_chatroom_created", "chat_reactions_message")
    ),
)


# Schema version stored in PRAGMA user_version. To change the schema: bump SCHEMA_VERSION
# and add MIGRATIONS[new_version] = (statements...) upgrading from the previous version.
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, tuple[str, ...]] = {}


class Database:

    def __init__(self, hass: HomeAssistant, path: Path) -> None:
        self._hass = hass
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.created = False

    # Sync API (executor only).

    def open(self) -> None:
        self.created = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            for statement in SCHEMA:
                conn.execute(statement)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(target, ()):
                    conn.execute(statement)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except sqlite3.Error:
            conn.close()
            raise
        with self._lock:
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.executemany(sql, [params])

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany(sql, rows)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                _LOGGER.exception("Sentinel database write failed: %s", sql.split("(")[0].strip())

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self._lock:
            if self._conn is None:
                return []
            return self._conn.execute(sql, params).fetchall()

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # Async wrappers.

    async def async_open(self) -> None:
        await self._hass.async_add_executor_job(self.open)

    async def async_close(self) -> None:
        await self._hass.async_add_executor_job(self.close)

    async def async_execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self._hass.async_add_executor_job(self.execute, sql, params)

    async def async_executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await self._hass.async_add_executor_job(self.executemany, sql, list(rows))

    async def async_query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return await self._hass.async_add_executor_job(self.query, sql, params)
