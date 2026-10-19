from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import CONF_CALENDAR_NOTIFICATIONS, DEFAULT_ALL_DAY_TIME
from .dispatch import async_send_notification
from .routing import parse_time
from .runtime import SentinelData
from .scheduler import trigger_notification_data

_LOGGER = logging.getLogger(__name__)


def _event_key(state: State) -> tuple:
    attrs = state.attributes
    return (attrs.get("message"), attrs.get("start_time"), attrs.get("end_time"))


def _notification_data(item: dict[str, Any], state: State) -> dict[str, Any]:
    attrs = state.attributes
    return trigger_notification_data(
        {
            **item,
            "title": item.get("title") or attrs.get("message") or item["name"],
            "message": item.get("message") or attrs.get("description") or attrs.get("message") or "",
        },
        "calendar",
    )


@callback
def async_setup_calendar_notifications(hass: HomeAssistant, runtime: SentinelData) -> Callable[[], None]:
    unsubs: list[Callable[[], None]] = []
    pending_all_day: dict[str, Callable[[], None]] = {}

    def _cancel_pending(item_id: str) -> None:
        if unsub := pending_all_day.pop(item_id, None):
            unsub()

    async def _fire(item: dict[str, Any], state: State) -> None:
        try:
            await async_send_notification(hass, runtime, _notification_data(item, state))
        except Exception:
            _LOGGER.exception("Sentinel: calendar notification '%s' failed", item.get("name"))

    async def _handle_active(item: dict[str, Any], state: State, *, startup: bool = False) -> None:
        """Fire now, or at all_day_time for all-day events. At startup a passed time is skipped."""
        if not state.attributes.get("all_day"):
            await _fire(item, state)
            return
        fire_at = parse_time(item.get("all_day_time")) or parse_time(DEFAULT_ALL_DAY_TIME)
        now = dt_util.now()
        target = now.replace(hour=fire_at.hour, minute=fire_at.minute, second=fire_at.second, microsecond=0)
        if target <= now:
            if not startup:
                await _fire(item, state)
            return
        _cancel_pending(item["id"])
        key = _event_key(state)

        async def _fire_if_still_active(_now: datetime) -> None:
            pending_all_day.pop(item["id"], None)
            current = hass.states.get(item["calendar_entity"])
            if current is not None and current.state == STATE_ON and _event_key(current) == key:
                await _fire(item, current)

        pending_all_day[item["id"]] = async_track_point_in_time(
            hass, _fire_if_still_active, dt_util.as_utc(target)
        )

    def _watch(item: dict[str, Any]) -> None:
        entity_id = item.get("calendar_entity")
        if not entity_id:
            return

        async def _on_state_change(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            old_state = event.data["old_state"]
            if new_state is None or new_state.state != STATE_ON:
                return
            # Fire on off->on, and on a new event replacing the active one (back-to-back events).
            if old_state is not None and old_state.state == STATE_ON and _event_key(old_state) == _event_key(new_state):
                return
            await _handle_active(item, new_state)

        unsubs.append(async_track_state_change_event(hass, [entity_id], _on_state_change))
        current = hass.states.get(entity_id)
        if current is not None and current.state == STATE_ON and current.attributes.get("all_day"):
            hass.async_create_task(_handle_active(item, current, startup=True))

    for item in runtime.options.get(CONF_CALENDAR_NOTIFICATIONS, []):
        if item.get("enabled", True):
            _watch(item)

    @callback
    def _unsub_all() -> None:
        for unsub in [*unsubs, *pending_all_day.values()]:
            unsub()
        unsubs.clear()
        pending_all_day.clear()

    return _unsub_all
