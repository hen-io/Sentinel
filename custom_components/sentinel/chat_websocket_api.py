"""Chat websocket commands (sentinel/chat/<name>)."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .chat_dispatch import (
    async_mark_chatroom_read,
    async_post_chat_message,
    async_toggle_reaction,
    group_reactions,
    room_accessible,
)
from .chat_identity import persons_by_user, resolve_sender
from .const import (
    CHAT_MESSAGE_MAX_LENGTH,
    CONF_CHAT_REACTION_EMOJI,
    CONF_CHATROOMS,
    DEFAULT_CHAT_REACTION_EMOJI,
    SIGNAL_CHAT_MESSAGE,
    SIGNAL_CHAT_REACTION,
    SIGNAL_CHAT_READ,
)
from .images import MAX_CHAT_IMAGE_BYTES, async_save_chat_image
from .runtime import SentinelData, get_runtime
from .ws import ws_command

Connection = websocket_api.ActiveConnection

_PUBLIC_ROOM_FIELDS = ("id", "name", "icon", "navigate_url")


def _reaction_emoji(runtime: SentinelData) -> list[str]:
    return runtime.options.get(CONF_CHAT_REACTION_EMOJI) or list(DEFAULT_CHAT_REACTION_EMOJI)


def _find_room(runtime: SentinelData, chatroom_id: str, user_id: str) -> dict | None:
    """Enabled room the user may access, else None."""
    room = next((c for c in runtime.options.get(CONF_CHATROOMS, []) if c["id"] == chatroom_id), None)
    if room is None or not room.get("enabled", True) or not room_accessible(room, user_id):
        return None
    return room


def _deny(connection: Connection, msg: dict) -> None:
    connection.send_error(msg["id"], "access_denied", "No access to this chatroom")


def _serialize(runtime: SentinelData, message: dict, persons: dict[str, State]) -> dict:
    return {
        **message,
        "sender": resolve_sender(message, runtime.user_names, persons),
        "reactions": group_reactions(runtime.chat_store.async_get_reactions(message["id"])),
    }


@ws_command("chat/list_rooms")
def _list_rooms(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    user_id = connection.user.id
    rooms = [
        {
            **{key: room.get(key) for key in _PUBLIC_ROOM_FIELDS},
            "unread_count": runtime.chat_store.async_unread_count(room["id"], user_id),
        }
        for room in runtime.options.get(CONF_CHATROOMS, [])
        if room.get("enabled", True) and room_accessible(room, user_id)
    ]
    connection.send_result(msg["id"], {"chatrooms": rooms, "reaction_emoji": _reaction_emoji(runtime)})


@ws_command(
    "chat/list_messages",
    {
        vol.Required("chatroom_id"): str,
        vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=1000)),
        vol.Optional("before_id"): str,
    },
)
def _list_messages(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    if _find_room(runtime, msg["chatroom_id"], connection.user.id) is None:
        _deny(connection, msg)
        return
    store = runtime.chat_store
    persons = persons_by_user(hass)
    messages = store.async_list_messages(msg["chatroom_id"], limit=msg.get("limit"), before_id=msg.get("before_id"))
    read_states = [
        {**rs, "name": runtime.user_names.get(rs["user_id"], "Unknown user")}
        for rs in store.async_room_read_states(msg["chatroom_id"])
    ]
    connection.send_result(
        msg["id"],
        {
            "messages": [_serialize(runtime, m, persons) for m in messages],
            "read_states": read_states,
            "reaction_emoji": _reaction_emoji(runtime),
        },
    )


@ws_command("chat/subscribe", {vol.Required("chatroom_id"): str})
def _subscribe(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    chatroom_id = msg["chatroom_id"]
    if _find_room(runtime, chatroom_id, connection.user.id) is None:
        _deny(connection, msg)
        return

    def _send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    @callback
    def forward_message(payload: dict) -> None:
        if payload["chatroom_id"] == chatroom_id and (current := get_runtime(hass)):
            _send({"message": _serialize(current, payload["message"], persons_by_user(hass))})

    @callback
    def forward_read(payload: dict) -> None:
        if payload["chatroom_id"] == chatroom_id:
            _send({"read": {k: payload[k] for k in ("user_id", "name", "last_read_at")}})

    @callback
    def forward_reaction(payload: dict) -> None:
        if payload["chatroom_id"] == chatroom_id:
            _send({"reaction": {"message_id": payload["message_id"], "reactions": payload["reactions"]}})

    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_CHAT_MESSAGE, forward_message),
        async_dispatcher_connect(hass, SIGNAL_CHAT_READ, forward_read),
        async_dispatcher_connect(hass, SIGNAL_CHAT_REACTION, forward_reaction),
    ]

    @callback
    def unsub() -> None:
        for fn in unsubs:
            fn()

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])


@ws_command(
    "chat/send",
    {
        vol.Required("chatroom_id"): str,
        vol.Required("message"): vol.All(str, vol.Length(max=CHAT_MESSAGE_MAX_LENGTH)),
        vol.Optional("image_base64"): vol.All(str, vol.Length(max=MAX_CHAT_IMAGE_BYTES * 4 // 3 + 4)),
        # Accepted for older cards; the real type is detected from the image bytes.
        vol.Optional("image_content_type"): str,
    },
)
async def _send(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    room = _find_room(runtime, msg["chatroom_id"], connection.user.id)
    if room is None:
        _deny(connection, msg)
        return
    text = msg["message"].strip()
    message_id = str(uuid.uuid4())
    image_url = None
    if msg.get("image_base64"):
        image_url = await async_save_chat_image(hass, room["id"], message_id, msg["image_base64"])
        if image_url is None:
            connection.send_error(msg["id"], "invalid_image", "Unsupported or invalid image")
            return
    if not text and not image_url:
        connection.send_error(msg["id"], "empty_message", "Message is empty")
        return
    message = await async_post_chat_message(
        hass,
        runtime,
        room,
        {
            "id": message_id,
            "message": text,
            "sender_user_id": connection.user.id,
            "source": "card",
            "image": image_url,
        },
    )
    connection.send_result(msg["id"], {"message": _serialize(runtime, message, persons_by_user(hass))})


@ws_command("chat/mark_read", {vol.Required("chatroom_id"): str})
async def _mark_read(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    if _find_room(runtime, msg["chatroom_id"], connection.user.id) is None:
        _deny(connection, msg)
        return
    await async_mark_chatroom_read(hass, runtime, msg["chatroom_id"], connection.user.id)
    connection.send_result(msg["id"])


@ws_command(
    "chat/toggle_reaction",
    {
        vol.Required("chatroom_id"): str,
        vol.Required("message_id"): str,
        vol.Required("emoji"): vol.All(str, vol.Length(min=1, max=32)),
    },
)
async def _toggle_reaction(hass: HomeAssistant, connection: Connection, msg: dict, runtime: SentinelData) -> None:
    if _find_room(runtime, msg["chatroom_id"], connection.user.id) is None:
        _deny(connection, msg)
        return
    if not runtime.chat_store.async_message_belongs_to_room(msg["chatroom_id"], msg["message_id"]):
        connection.send_error(msg["id"], "not_found", "No such message in this chatroom")
        return
    reactions = await async_toggle_reaction(
        hass, runtime, msg["chatroom_id"], msg["message_id"], connection.user.id, msg["emoji"]
    )
    connection.send_result(msg["id"], {"reactions": reactions})
