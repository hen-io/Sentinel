from __future__ import annotations

import logging
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CHAT_ALERT_LIVE_ID_PREFIX,
    CHAT_CHANNEL,
    CONF_CHANNELS,
    CONF_RECIPIENTS,
    SIGNAL_CHAT_MESSAGE,
    SIGNAL_CHAT_REACTION,
    SIGNAL_CHAT_READ,
)
from .dispatch import async_acknowledge, async_send_notification
from .models import ChatMessage
from .routing import channel_accepts, importance_rank
from .runtime import SentinelData

_LOGGER = logging.getLogger(__name__)

_MESSAGE_PREVIEW_MAX_LEN = 120
_CHAT_ALERT_IMPORTANCE = "normal"
_warned_recipients: set[str] = set()


def chat_alert_live_id(chatroom_id: str, user_id: str) -> str:
    return f"{CHAT_ALERT_LIVE_ID_PREFIX}{chatroom_id}_{user_id}"


def room_accessible(chatroom: dict[str, Any], user_id: str | None) -> bool:
    if chatroom.get("access_mode") == "selected":
        return user_id in (chatroom.get("allowed_user_ids") or [])
    return True


async def _room_members(hass: HomeAssistant, chatroom: dict[str, Any]) -> list[str]:
    if chatroom.get("access_mode") == "selected":
        return list(chatroom.get("allowed_user_ids") or [])
    return [u.id for u in await hass.auth.async_get_users() if not u.system_generated]


def _linked_recipients(runtime: SentinelData, user_id: str) -> list[dict[str, Any]]:
    return [
        r
        for r in runtime.options.get(CONF_RECIPIENTS, [])
        if r.get("linked_user_id") == user_id and r.get("enabled", True)
    ]


def _warn_if_filtered(runtime: SentinelData, recipient: dict[str, Any]) -> None:
    """Explain (once per recipient) why a linked recipient would never get chat alerts."""
    if recipient["id"] in _warned_recipients:
        return
    chat = next((c for c in runtime.options.get(CONF_CHANNELS, []) if c["key"] == CHAT_CHANNEL), None)
    if chat is None or channel_accepts(recipient, chat, importance_rank(_CHAT_ALERT_IMPORTANCE), False):
        return
    _warned_recipients.add(recipient["id"])
    _LOGGER.warning(
        "Sentinel chat alert: recipient '%s' filters out the 'chat' channel at '%s' importance "
        "(disabled channel, not in Allowed channels, or minimum importance too high)",
        recipient.get("name", recipient["id"]),
        _CHAT_ALERT_IMPORTANCE,
    )


async def async_post_chat_message(
    hass: HomeAssistant, runtime: SentinelData, chatroom: dict[str, Any], data: dict[str, Any]
) -> ChatMessage:
    message: ChatMessage = {
        "id": data.get("id") or str(uuid.uuid4()),
        "chatroom_id": chatroom["id"],
        "sender_user_id": data.get("sender_user_id"),
        "sender_name": data.get("sender_name"),
        "sender_icon": data.get("sender_icon"),
        "message": data["message"],
        "created": dt_util.utcnow().isoformat(),
        "source": data.get("source"),
        "image": data.get("image"),
    }
    await runtime.chat_store.async_add_message(message)
    if message["sender_user_id"]:
        await async_mark_chatroom_read(hass, runtime, chatroom["id"], message["sender_user_id"])
    async_dispatcher_send(hass, SIGNAL_CHAT_MESSAGE, {"chatroom_id": chatroom["id"], "message": message})
    if chatroom.get("new_message_alert"):
        await _send_chat_alerts(hass, runtime, chatroom, message)
    return message


async def _send_chat_alerts(
    hass: HomeAssistant, runtime: SentinelData, chatroom: dict[str, Any], message: ChatMessage
) -> None:
    preview = message["message"] or "📷"
    if len(preview) > _MESSAGE_PREVIEW_MAX_LEN:
        preview = preview[: _MESSAGE_PREVIEW_MAX_LEN - 1] + "…"

    for member_user_id in await _room_members(hass, chatroom):
        if member_user_id == message.get("sender_user_id"):
            continue
        if runtime.chat_store.async_unread_count(chatroom["id"], member_user_id) <= 0:
            continue
        recipients = _linked_recipients(runtime, member_user_id)
        if not recipients:
            _LOGGER.debug("Sentinel chat alert: user %s has no linked recipient", member_user_id)
            continue
        for recipient in recipients:
            _warn_if_filtered(runtime, recipient)

        alert: dict[str, Any] = {
            "title": chatroom["name"],
            "message": preview,
            "channel": CHAT_CHANNEL,
            "importance": _CHAT_ALERT_IMPORTANCE,
            "target_recipients": [r["id"] for r in recipients],
            "live_id": chat_alert_live_id(chatroom["id"], member_user_id),
            "persistent": False,
            "show_in_history": False,
            "source": f"chat:{chatroom['id']}",
            "icon": chatroom.get("icon"),
        }
        if chatroom.get("navigate_url"):
            alert["data"] = {"clickAction": chatroom["navigate_url"]}
        await async_send_notification(hass, runtime, alert)


async def async_mark_chatroom_read(
    hass: HomeAssistant, runtime: SentinelData, chatroom_id: str, user_id: str
) -> None:
    latest = runtime.chat_store.async_latest_message(chatroom_id)
    at = dt_util.utcnow().isoformat()
    await runtime.chat_store.async_mark_read(chatroom_id, user_id, latest["id"] if latest else None, at)
    async_dispatcher_send(
        hass,
        SIGNAL_CHAT_READ,
        {
            "chatroom_id": chatroom_id,
            "user_id": user_id,
            "name": runtime.user_names.get(user_id, "Unknown user"),
            "last_read_at": at,
        },
    )
    record = runtime.store.async_get_by_live_id(chat_alert_live_id(chatroom_id, user_id))
    if record is not None:
        await async_acknowledge(hass, runtime, record["id"], "chat_read")


def group_reactions(raw: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for row in raw:
        grouped.setdefault(row["emoji"], []).append(row["user_id"])
    return [{"emoji": e, "count": len(ids), "user_ids": ids} for e, ids in grouped.items()]


async def async_toggle_reaction(
    hass: HomeAssistant, runtime: SentinelData, chatroom_id: str, message_id: str, user_id: str, emoji: str
) -> list[dict[str, Any]]:
    store = runtime.chat_store
    if any(r["user_id"] == user_id and r["emoji"] == emoji for r in store.async_get_reactions(message_id)):
        await store.async_remove_reaction(message_id, user_id, emoji)
    else:
        await store.async_add_reaction(message_id, user_id, emoji)
    reactions = group_reactions(store.async_get_reactions(message_id))
    async_dispatcher_send(
        hass,
        SIGNAL_CHAT_REACTION,
        {"chatroom_id": chatroom_id, "message_id": message_id, "reactions": reactions},
    )
    return reactions
