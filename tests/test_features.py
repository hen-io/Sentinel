"""Reminders, chat message deletion and options flow behaviour."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.sentinel.const import DOMAIN

from .conftest import make_options


async def _send(hass: HomeAssistant, **data) -> dict:
    data = {"title": "T", "message": "M", **data}
    return await hass.services.async_call(DOMAIN, "send_notification", data, blocking=True, return_response=True)


async def test_reminders_repeat_until_limit(hass: HomeAssistant, loaded_entry, notify_calls):
    response = await _send(hass, persistent=True, repeat_minutes=1, repeat_count=2)
    assert len(notify_calls) == 1
    for minutes in (1, 2, 3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
        await hass.async_block_till_done()
    assert len(notify_calls) == 3, "initial push + 2 repeats"
    assert loaded_entry.runtime_data.store.async_get(response["id"])["repeats_sent"] == 2


async def test_reminders_stop_on_acknowledge(hass: HomeAssistant, loaded_entry, notify_calls):
    response = await _send(hass, persistent=True, repeat_minutes=1)
    await hass.services.async_call(DOMAIN, "acknowledge", {"notification_id": response["id"]}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    pushes = [c for c in notify_calls if c.data["message"] != "clear_notification"]
    assert len(pushes) == 1


async def test_chat_delete_own_message(hass: HomeAssistant, notify_calls, hass_ws_client):
    room = {"id": "room1", "name": "Family", "access_mode": "all", "enabled": True}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=make_options(chatrooms=[room]))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/chat/send", "chatroom_id": "room1", "message": "hi"})
    message_id = (await client.receive_json())["result"]["message"]["id"]
    await client.send_json_auto_id({"type": f"{DOMAIN}/chat/delete_message", "chatroom_id": "room1", "message_id": message_id})
    assert (await client.receive_json())["success"]
    assert entry.runtime_data.chat_store.async_get_message("room1", message_id) is None

    await client.send_json_auto_id({"type": f"{DOMAIN}/chat/delete_message", "chatroom_id": "room1", "message_id": message_id})
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def _options_step(hass: HomeAssistant, entry: MockConfigEntry, menu_choice: str, select: dict) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": menu_choice})
    return await hass.config_entries.options.async_configure(result["flow_id"], select)


async def test_options_menu_summary_and_channel_rename(hass: HomeAssistant, loaded_entry):
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["description_placeholders"]["recipient"] == "1"

    options = dict(loaded_entry.options)
    options["recipients"] = [{**options["recipients"][0], "allowed_channels": ["security"], "channel_importance": {"security": "low"}}]
    hass.config_entries.async_update_entry(loaded_entry, options=options)
    await hass.async_block_till_done()

    security = next(c for c in loaded_entry.options["channels"] if c["key"] == "security")
    result = await _options_step(hass, loaded_entry, "manage_channels", {"channel": security["id"]})
    assert result["step_id"] == "edit_channel"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"key": "Alarm", "name": "Alarm", "min_importance": "high", "enabled": True, "labels": ["Front door"]},
    )
    assert result["type"] is FlowResultType.MENU
    await hass.async_block_till_done()
    recipient = loaded_entry.options["recipients"][0]
    assert recipient["allowed_channels"] == ["alarm"]
    assert recipient["channel_importance"] == {"alarm": "low"}


async def test_options_recipient_channel_importance(hass: HomeAssistant, loaded_entry):
    result = await _options_step(hass, loaded_entry, "manage_recipients", {"recipient": "r1"})
    assert result["step_id"] == "edit_recipient"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Phone",
            "notify_service": "mobile_app_phone",
            "min_importance": "high",
            "channel_importance": ["security:low", "info:critical"],
            "enabled": True,
        },
    )
    assert result["type"] is FlowResultType.MENU
    await hass.async_block_till_done()
    recipient = loaded_entry.options["recipients"][0]
    assert recipient["channel_importance"] == {"security": "low", "info": "critical"}
    assert recipient["linked_user_id"] is None
