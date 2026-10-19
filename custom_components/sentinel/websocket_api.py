"""Notification websocket commands (sentinel/<name>)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_CHANNELS, IMPORTANCE_LEVELS, SIGNAL_CONFIG_UPDATED, SIGNAL_DELETED, SIGNAL_UPDATED
from .dispatch import async_acknowledge, async_delete_notifications
from .runtime import SentinelData
from .ws import ws_command

Connection = websocket_api.ActiveConnection


def config_payload(options: dict[str, Any]) -> dict[str, Any]:
    """Public channel/label config for the cards."""
    return {
        "importance_levels": IMPORTANCE_LEVELS,
        "channels": [
            {
                "key": c["key"],
                "name": c["name"],
                "min_importance": c.get("min_importance", "low"),
                "enabled": c.get("enabled", True),
                "color": c.get("color"),
                "labels": [{"key": l["key"], "name": l["name"]} for l in c.get("labels") or []],
            }
            for c in options.get(CONF_CHANNELS, [])
        ],
    }


@ws_command(
    "list",
    {
        vol.Optional("include_acknowledged", default=True): bool,
        vol.Optional("limit"): vol.All(int, vol.Range(min=1)),
        vol.Optional("channels"): [str],
        vol.Optional("channel_mode", default="include"): vol.In(["include", "exclude"]),
        vol.Optional("min_importance"): vol.In(IMPORTANCE_LEVELS),
        vol.Optional("labels"): [str],
        vol.Optional("outstanding_only", default=False): bool,
    },
)
def _list(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    records = runtime.store.async_list(
        include_acknowledged=msg["include_acknowledged"],
        limit=msg.get("limit"),
        channels=msg.get("channels"),
        channel_mode=msg["channel_mode"],
        min_importance=msg.get("min_importance"),
        labels=msg.get("labels"),
        outstanding_only=msg["outstanding_only"],
    )
    connection.send_result(msg["id"], {"notifications": records})


@ws_command("config")
def _config(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    connection.send_result(msg["id"], config_payload(runtime.options))


@ws_command("subscribe")
def _subscribe(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    @callback
    def forward_updated(record: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"notification": record}))

    @callback
    def forward_deleted(deleted_ids: list[str]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"deleted_ids": deleted_ids}))

    @callback
    def forward_config(config: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"config": config}))

    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_UPDATED, forward_updated),
        async_dispatcher_connect(hass, SIGNAL_DELETED, forward_deleted),
        async_dispatcher_connect(hass, SIGNAL_CONFIG_UPDATED, forward_config),
    ]

    @callback
    def unsub() -> None:
        for fn in unsubs:
            fn()

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])


@ws_command("acknowledge", {vol.Required("notification_id"): str})
async def _acknowledge(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    record = await async_acknowledge(hass, runtime, msg["notification_id"], "card")
    if record is None:
        connection.send_error(msg["id"], "not_found", "Notification not found")
        return
    connection.send_result(msg["id"], {"notification": record})


@ws_command("delete", {vol.Required("notification_ids"): [str]})
async def _delete(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    deleted = await async_delete_notifications(hass, runtime, msg["notification_ids"])
    connection.send_result(msg["id"], {"deleted_ids": deleted})


@ws_command("clear_history", {vol.Optional("include_unacknowledged", default=False): bool})
async def _clear_history(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    ids = runtime.store.async_history_ids(msg["include_unacknowledged"])
    deleted = await async_delete_notifications(hass, runtime, ids)
    connection.send_result(msg["id"], {"deleted_ids": deleted})
