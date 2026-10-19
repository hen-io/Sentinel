from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .database import Database
from .models import ChatMessage

_UPSERT_MESSAGE_SQL = """
    INSERT INTO chat_messages (id, chatroom_id, sender_user_id, created, message_json)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        chatroom_id = excluded.chatroom_id,
        sender_user_id = excluded.sender_user_id,
        created = excluded.created,
        message_json = excluded.message_json
"""


class ChatStore:
    """In-memory chat state (messages newest first per room) backed by the database."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._messages: dict[str, list[ChatMessage]] = {}
        self._reads: dict[tuple[str, str], dict[str, str | None]] = {}
        self._reactions: dict[str, list[dict[str, str]]] = {}

    async def async_load(self) -> None:
        message_rows = await self._db.async_query(
            "SELECT message_json FROM chat_messages ORDER BY created DESC"
        )
        read_rows = await self._db.async_query(
            "SELECT chatroom_id, user_id, last_read_message_id, last_read_at FROM chat_reads"
        )
        reaction_rows = await self._db.async_query(
            "SELECT message_id, user_id, emoji FROM chat_reactions ORDER BY created"
        )
        self._messages = {}
        for (blob,) in message_rows:
            message: ChatMessage = json.loads(blob)
            self._messages.setdefault(message["chatroom_id"], []).append(message)
        self._reads = {
            (room_id, user_id): {"last_read_message_id": message_id, "last_read_at": at}
            for room_id, user_id, message_id, at in read_rows
        }
        self._reactions = {}
        for message_id, user_id, emoji in reaction_rows:
            self._reactions.setdefault(message_id, []).append({"user_id": user_id, "emoji": emoji})

    def async_all_messages(self) -> list[ChatMessage]:
        return [m for messages in self._messages.values() for m in messages]

    async def async_add_message(self, message: ChatMessage) -> None:
        self._messages.setdefault(message["chatroom_id"], []).insert(0, message)
        await self.async_update_message(message)

    async def async_update_message(self, message: ChatMessage) -> None:
        await self._db.async_execute(
            _UPSERT_MESSAGE_SQL,
            (
                message["id"],
                message["chatroom_id"],
                message.get("sender_user_id"),
                message["created"],
                json.dumps(message),
            ),
        )

    def async_list_messages(
        self, chatroom_id: str, limit: int | None = None, before_id: str | None = None
    ) -> list[ChatMessage]:
        messages = self._messages.get(chatroom_id, [])
        if before_id:
            cursor = next((m for m in messages if m["id"] == before_id), None)
            if cursor:
                messages = [m for m in messages if m["created"] < cursor["created"]]
        return messages[:limit] if limit is not None else messages

    def async_latest_message(self, chatroom_id: str) -> ChatMessage | None:
        messages = self._messages.get(chatroom_id, [])
        return messages[0] if messages else None

    def async_message_belongs_to_room(self, chatroom_id: str, message_id: str) -> bool:
        return any(m["id"] == message_id for m in self._messages.get(chatroom_id, []))

    def async_get_reactions(self, message_id: str) -> list[dict[str, str]]:
        return list(self._reactions.get(message_id, []))

    async def async_add_reaction(self, message_id: str, user_id: str, emoji: str) -> None:
        existing = self._reactions.setdefault(message_id, [])
        if not any(r["user_id"] == user_id and r["emoji"] == emoji for r in existing):
            existing.append({"user_id": user_id, "emoji": emoji})
        await self._db.async_execute(
            "INSERT OR IGNORE INTO chat_reactions (message_id, user_id, emoji, created) "
            "VALUES (?, ?, ?, ?)",
            (message_id, user_id, emoji, dt_util.utcnow().isoformat()),
        )

    async def async_remove_reaction(self, message_id: str, user_id: str, emoji: str) -> None:
        existing = self._reactions.get(message_id)
        if existing:
            self._reactions[message_id] = [
                r for r in existing if not (r["user_id"] == user_id and r["emoji"] == emoji)
            ]
        await self._db.async_execute(
            "DELETE FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (message_id, user_id, emoji),
        )

    def async_room_read_states(self, chatroom_id: str) -> list[dict[str, str | None]]:
        return [
            {"user_id": user_id, "last_read_at": watermark["last_read_at"]}
            for (room_id, user_id), watermark in self._reads.items()
            if room_id == chatroom_id
        ]

    async def async_mark_read(
        self, chatroom_id: str, user_id: str, message_id: str | None, at: str
    ) -> None:
        self._reads[(chatroom_id, user_id)] = {"last_read_message_id": message_id, "last_read_at": at}
        await self._db.async_execute(
            """
            INSERT INTO chat_reads (chatroom_id, user_id, last_read_message_id, last_read_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chatroom_id, user_id) DO UPDATE SET
                last_read_message_id = excluded.last_read_message_id,
                last_read_at = excluded.last_read_at
            """,
            (chatroom_id, user_id, message_id, at),
        )

    def async_unread_count(self, chatroom_id: str, user_id: str) -> int:
        messages = self._messages.get(chatroom_id, [])
        watermark = self._reads.get((chatroom_id, user_id))
        if watermark is None:
            return len(messages)
        last_read_at = watermark["last_read_at"] or ""
        return sum(1 for m in messages if m["created"] > last_read_at)

    async def async_prune(
        self,
        chatroom_defs: list[dict[str, Any]],
        default_max_days: int,
        default_max_messages: int,
    ) -> dict[str, list[str]]:
        """Drop messages past room limits; returns removed message ids per room."""
        lookup = {c["id"]: c for c in chatroom_defs}
        now = dt_util.utcnow()
        removed_by_room: dict[str, list[str]] = {}

        for chatroom_id, messages in self._messages.items():
            room = lookup.get(chatroom_id) or {}
            max_days = room.get("history_max_days") or default_max_days
            max_messages = room.get("history_max_messages") or default_max_messages
            cutoff = now - timedelta(days=max_days)
            kept = [
                m
                for m in messages
                if (created := dt_util.parse_datetime(m["created"])) is not None and created >= cutoff
            ][:max_messages]
            if len(kept) != len(messages):
                kept_ids = {m["id"] for m in kept}
                removed_by_room[chatroom_id] = [m["id"] for m in messages if m["id"] not in kept_ids]
                self._messages[chatroom_id] = kept

        removed = [i for ids in removed_by_room.values() for i in ids]
        if removed:
            for message_id in removed:
                self._reactions.pop(message_id, None)
            rows = [(i,) for i in removed]
            await self._db.async_executemany("DELETE FROM chat_messages WHERE id = ?", rows)
            await self._db.async_executemany("DELETE FROM chat_reactions WHERE message_id = ?", rows)
        return removed_by_room
