from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .chat_dispatch import async_post_chat_message, room_accessible
from .config_snapshot import async_restore_config_from_snapshot
from .const import (
    CHAT_MESSAGE_MAX_LENGTH,
    CONF_CHATROOMS,
    DEFAULT_CHANNEL,
    DEFAULT_IMPORTANCE,
    DOMAIN,
    IMPORTANCE_LEVELS,
)
from .dispatch import async_acknowledge, async_delete_notifications, async_send_notification
from .runtime import SentinelData, require_runtime

ACTION_SCHEMA = vol.Schema(
    {vol.Required("action"): cv.string, vol.Required("title"): cv.string},
    extra=vol.ALLOW_EXTRA,
)

SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("icon"): cv.string,
        vol.Optional("image"): cv.string,
        vol.Optional("image_entity"): cv.entity_id,
        vol.Optional("channel", default=DEFAULT_CHANNEL): vol.Any(cv.string, [cv.string]),
        vol.Optional("labels", default=list): vol.Any(cv.string, [cv.string]),
        vol.Optional("importance", default=DEFAULT_IMPORTANCE): vol.In(IMPORTANCE_LEVELS),
        vol.Optional("actions", default=list): [ACTION_SCHEMA],
        vol.Optional("persistent", default=False): cv.boolean,
        vol.Optional("data", default=dict): dict,
        vol.Optional("target_recipients"): [cv.string],
        vol.Optional("home_only", default=False): cv.boolean,
        vol.Optional("live_id"): cv.string,
        vol.Optional("interactive_only", default=False): cv.boolean,
        vol.Optional("source"): cv.string,
        vol.Optional("show_in_history", default=True): cv.boolean,
        vol.Optional("dismiss_on_action", default=False): cv.boolean,
        vol.Optional("clear_on_acknowledge", default=True): cv.boolean,
    }
)


async def _send_notification(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> ServiceResponse:
    record = await async_send_notification(hass, runtime, dict(call.data))
    return {
        "id": record["id"],
        "live_id": record["live_id"],
        "delivered_to": [rid for rid, d in record["recipients"].items() if d.get("dispatched")],
    }


async def _acknowledge(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    await async_acknowledge(hass, runtime, call.data["notification_id"], "service")


async def _end_live_notification(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    if record := runtime.store.async_get_by_live_id(call.data["live_id"]):
        await async_acknowledge(hass, runtime, record["id"], "live_ended")


async def _delete_notification(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    await async_delete_notifications(hass, runtime, [call.data["notification_id"]])


async def _clear_history(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    ids = runtime.store.async_history_ids(call.data["include_unacknowledged"])
    await async_delete_notifications(hass, runtime, ids)


async def _restore_config(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    if not call.data["confirm"]:
        raise ServiceValidationError("Set confirm: true to replace the whole configuration")
    await async_restore_config_from_snapshot(hass, runtime)


async def _send_chat_message(hass: HomeAssistant, runtime: SentinelData, call: ServiceCall) -> None:
    query = call.data["chatroom_id"].strip().lower()
    chatroom = next(
        (
            c
            for c in runtime.options.get(CONF_CHATROOMS, [])
            if c["id"] == call.data["chatroom_id"] or c["name"].strip().lower() == query
        ),
        None,
    )
    if chatroom is None:
        raise ServiceValidationError(f"No chatroom '{call.data['chatroom_id']}'")

    user_id = call.context.user_id
    if user_id and not room_accessible(chatroom, user_id):
        user = await hass.auth.async_get_user(user_id)
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)

    sender_name = call.data.get("sender_name")
    sender_icon = call.data.get("sender_icon")
    if sender_name or sender_icon:
        user_id = None
    await async_post_chat_message(
        hass,
        runtime,
        chatroom,
        {
            "message": call.data["message"],
            "sender_user_id": user_id,
            "sender_name": None if user_id else (sender_name or "System"),
            "sender_icon": None if user_id else (sender_icon or "mdi:robot"),
            "source": "service",
        },
    )


Handler = Callable[[HomeAssistant, SentinelData, ServiceCall], Awaitable[ServiceResponse | None]]


@dataclass(frozen=True, slots=True)
class ServiceSpec:

    name: str
    handler: Handler
    schema: vol.Schema
    response: SupportsResponse = SupportsResponse.NONE
    admin: bool = False


SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("send_notification", _send_notification, SEND_NOTIFICATION_SCHEMA, SupportsResponse.OPTIONAL),
    ServiceSpec("acknowledge", _acknowledge, vol.Schema({vol.Required("notification_id"): cv.string})),
    ServiceSpec("end_live_notification", _end_live_notification, vol.Schema({vol.Required("live_id"): cv.string})),
    ServiceSpec("delete_notification", _delete_notification, vol.Schema({vol.Required("notification_id"): cv.string})),
    ServiceSpec(
        "clear_history",
        _clear_history,
        vol.Schema({vol.Optional("include_unacknowledged", default=False): cv.boolean}),
    ),
    ServiceSpec(
        "restore_config_from_snapshot",
        _restore_config,
        vol.Schema({vol.Required("confirm"): cv.boolean}),
        admin=True,
    ),
    ServiceSpec(
        "send_chat_message",
        _send_chat_message,
        vol.Schema(
            {
                vol.Required("chatroom_id"): cv.string,
                vol.Required("message"): vol.All(cv.string, vol.Length(min=1, max=CHAT_MESSAGE_MAX_LENGTH)),
                vol.Optional("sender_name"): cv.string,
                vol.Optional("sender_icon"): cv.string,
            }
        ),
    ),
)


def async_register_service(
    hass: HomeAssistant,
    domain: str,
    spec: ServiceSpec,
    on_call: Callable[[ServiceCall], None] | None = None,
) -> None:
    async def _handle(call: ServiceCall) -> ServiceResponse:
        if on_call is not None:
            on_call(call)
        return await spec.handler(hass, require_runtime(hass), call)

    if spec.admin:
        async_register_admin_service(hass, domain, spec.name, _handle, spec.schema)
    else:
        hass.services.async_register(domain, spec.name, _handle, spec.schema, supports_response=spec.response)


def async_register_services(hass: HomeAssistant) -> None:
    for spec in SERVICES:
        async_register_service(hass, DOMAIN, spec)
