from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from croniter import croniter
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import CONF_SCHEDULED_NOTIFICATIONS, DEFAULT_CHANNEL
from .dispatch import async_send_notification
from .runtime import SentinelData

_LOGGER = logging.getLogger(__name__)


def trigger_notification_data(item: dict[str, Any], source: str) -> dict[str, Any]:
    """send_notification data shared by scheduled and calendar notifications."""
    return {
        "title": item["title"],
        "message": item["message"],
        "icon": item.get("icon"),
        "channel": item.get("channel") or DEFAULT_CHANNEL,
        "labels": item.get("labels") or [],
        "importance": item.get("importance", "normal"),
        "persistent": item.get("persistent", False),
        "target_recipients": item.get("target_recipients") or None,
        "home_only": item.get("home_only", False),
        "source": f"{source}:{item.get('name') or item.get('id')}",
    }


@callback
def async_setup_scheduled_notifications(hass: HomeAssistant, runtime: SentinelData) -> Callable[[], None]:
    pending: dict[str, Callable[[], None]] = {}

    @callback
    def _schedule(item: dict[str, Any]) -> None:
        try:
            next_run = croniter(item["cron"], dt_util.now()).get_next(datetime)
        except Exception:
            _LOGGER.error("Sentinel: scheduled notification '%s' has an invalid cron '%s'", item.get("name"), item.get("cron"))
            return

        async def _fire(_now: datetime) -> None:
            pending.pop(item["id"], None)
            try:
                await async_send_notification(hass, runtime, trigger_notification_data(item, "scheduled"))
            except Exception:
                _LOGGER.exception("Sentinel: scheduled notification '%s' failed", item.get("name"))
            finally:
                _schedule(item)

        pending[item["id"]] = async_track_point_in_time(hass, _fire, dt_util.as_utc(next_run))

    for item in runtime.options.get(CONF_SCHEDULED_NOTIFICATIONS, []):
        if item.get("enabled", True):
            _schedule(item)

    @callback
    def _unsub_all() -> None:
        for unsub in pending.values():
            unsub()
        pending.clear()

    return _unsub_all
