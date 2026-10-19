from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .database import Database
from .models import NotificationRecord
from .routing import importance_rank, is_outstanding, record_channels

_UPSERT_SQL = """
    INSERT INTO notifications (id, live_id, channel, created, acknowledged, record_json)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        live_id = excluded.live_id,
        channel = excluded.channel,
        created = excluded.created,
        acknowledged = excluded.acknowledged,
        record_json = excluded.record_json
"""


def _row(record: NotificationRecord) -> tuple:
    return (
        record["id"],
        record.get("live_id"),
        record["channel"],
        record["created"],
        int(bool(record.get("acknowledged"))),
        json.dumps(record),
    )


class NotificationStore:
    """In-memory notification list (newest first) backed by the database."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._notifications: list[NotificationRecord] = []
        self._by_id: dict[str, NotificationRecord] = {}

    async def async_load(self) -> None:
        rows = await self._db.async_query(
            "SELECT record_json FROM notifications ORDER BY created DESC"
        )
        self._set_all([json.loads(row[0]) for row in rows])

    def _set_all(self, records: list[NotificationRecord]) -> None:
        for record in records:
            record.setdefault("labels", [])
        self._notifications = sorted(records, key=lambda r: r["created"], reverse=True)
        self._by_id = {r["id"]: r for r in self._notifications}

    def _insert_sorted(self, record: NotificationRecord) -> None:
        created = record["created"]
        index = next(
            (i for i, r in enumerate(self._notifications) if r["created"] < created),
            len(self._notifications),
        )
        self._notifications.insert(index, record)

    async def async_add(self, record: NotificationRecord) -> None:
        """Insert a new record or replace one with the same id."""
        previous = self._by_id.get(record["id"])
        if previous is not None:
            self._notifications.remove(previous)
        self._insert_sorted(record)
        self._by_id[record["id"]] = record
        await self._db.async_execute(_UPSERT_SQL, _row(record))

    async def async_add_many(self, records: list[NotificationRecord]) -> None:
        merged = {r["id"]: r for r in self._notifications}
        for record in records:
            merged.setdefault(record["id"], record)
        self._set_all(list(merged.values()))
        await self._db.async_executemany(_UPSERT_SQL, [_row(r) for r in records])

    async def async_update_record(self, record: NotificationRecord) -> None:
        await self._db.async_execute(_UPSERT_SQL, _row(record))

    def async_get(self, notification_id: str) -> NotificationRecord | None:
        return self._by_id.get(notification_id)

    def async_get_by_live_id(self, live_id: str) -> NotificationRecord | None:
        for record in self._notifications:
            if record.get("live_id") == live_id and not record["acknowledged"]:
                return record
        return None

    def async_list(
        self,
        include_acknowledged: bool = True,
        limit: int | None = None,
        channels: list[str] | None = None,
        channel_mode: str = "include",
        min_importance: str | None = None,
        labels: list[str] | None = None,
        outstanding_only: bool = False,
    ) -> list[NotificationRecord]:
        records: Any = self._notifications
        if outstanding_only:
            records = (r for r in records if r.get("requires_ack") and not r["acknowledged"])
        elif not include_acknowledged:
            records = (r for r in records if not r["acknowledged"])
        if channels:
            wanted = {c.strip().lower() for c in channels}
            exclude = channel_mode == "exclude"
            records = (
                r
                for r in records
                if bool({c.lower() for c in record_channels(r)} & wanted) != exclude
            )
        if labels:
            wanted_labels = {label.strip().lower() for label in labels}
            records = (r for r in records if wanted_labels & set(r.get("labels") or []))
        if min_importance:
            min_rank = importance_rank(min_importance)
            records = (r for r in records if importance_rank(r["importance"]) >= min_rank)
        records = list(records)
        return records[:limit] if limit is not None else records

    async def async_acknowledge(self, notification_id: str, via: str) -> NotificationRecord | None:
        record = self._by_id.get(notification_id)
        if record is None:
            return None
        record["acknowledged"] = True
        record["acknowledged_at"] = dt_util.utcnow().isoformat()
        record["acknowledged_via"] = via
        await self.async_update_record(record)
        return record

    async def async_delete(self, notification_ids: list[str]) -> list[str]:
        deleted = [i for i in dict.fromkeys(notification_ids) if i in self._by_id]
        if deleted:
            gone = set(deleted)
            self._notifications = [r for r in self._notifications if r["id"] not in gone]
            for notification_id in deleted:
                del self._by_id[notification_id]
            await self._db.async_executemany(
                "DELETE FROM notifications WHERE id = ?", [(i,) for i in deleted]
            )
        return deleted

    def async_history_ids(self, include_unacknowledged: bool = False) -> list[str]:
        if include_unacknowledged:
            return [r["id"] for r in self._notifications]
        return [r["id"] for r in self._notifications if not is_outstanding(r)]

    async def async_prune(
        self,
        channel_defs: list[dict[str, Any]],
        default_max_records: int,
        default_retention_days: int,
    ) -> list[str]:
        """Drop records past retention/count limits; outstanding ones are always kept."""
        lookup = {c["key"]: c for c in channel_defs}
        now = dt_util.utcnow()

        def _limit(key: str, field: str, default: int) -> int:
            return (lookup.get(key) or {}).get(field) or default

        counts: dict[str, int] = {}
        removed: list[str] = []
        for record in self._notifications:
            if is_outstanding(record):
                continue
            key = record["channel"]
            cutoff = now - timedelta(days=_limit(key, "retention_days", default_retention_days))
            created = dt_util.parse_datetime(record["created"])
            if created is None or created < cutoff or counts.get(key, 0) >= _limit(
                key, "max_records", default_max_records
            ):
                removed.append(record["id"])
                continue
            counts[key] = counts.get(key, 0) + 1
        return await self.async_delete(removed)
