"""Repeat pushes of unacknowledged notifications (repeat_minutes / repeat_count)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .models import NotificationRecord
from .store import NotificationStore

Resend = Callable[[NotificationRecord], Awaitable[None]]


def reminder_due(record: NotificationRecord) -> bool:
    if not record.get("repeat_minutes") or not record.get("requires_ack") or record.get("acknowledged"):
        return False
    limit = record.get("repeat_count")
    return limit is None or record.get("repeats_sent", 0) < limit


class ReminderManager:
    """One timer per outstanding notification that asks for reminders."""

    def __init__(self, hass: HomeAssistant, store: NotificationStore, resend: Resend) -> None:
        self._hass = hass
        self._store = store
        self._resend = resend
        self._timers: dict[str, CALLBACK_TYPE] = {}

    @callback
    def async_schedule(self, record: NotificationRecord) -> None:
        """(Re)start the reminder timer for a record, if it wants one."""
        self.async_cancel(record["id"])
        if not reminder_due(record):
            return
        job = HassJob(partial(self._async_fire, record["id"]), cancel_on_shutdown=True)
        self._timers[record["id"]] = async_call_later(self._hass, record["repeat_minutes"] * 60, job)

    @callback
    def async_cancel(self, record_id: str) -> None:
        if unsub := self._timers.pop(record_id, None):
            unsub()

    @callback
    def async_resume(self) -> None:
        for record in self._store.async_list(outstanding_only=True):
            self.async_schedule(record)

    @callback
    def async_shutdown(self) -> None:
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()

    async def _async_fire(self, record_id: str, _now: datetime | None = None) -> None:
        self._timers.pop(record_id, None)
        record = self._store.async_get(record_id)
        if record is None or not reminder_due(record):
            return
        await self._resend(record)
        self.async_schedule(record)
