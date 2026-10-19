from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.sentinel.const import DOMAIN
from custom_components.sentinel.legacy import LEGACY_ACK_ACTION_PREFIX, LEGACY_DOMAIN


async def _send(hass: HomeAssistant, domain: str = DOMAIN, **data) -> dict:
    data = {"title": "T", "message": "M", **data}
    return await hass.services.async_call(domain, "send_notification", data, blocking=True, return_response=True)


async def test_send_with_labels_and_response(hass: HomeAssistant, loaded_entry, notify_calls):
    response = await _send(hass, channel="security", labels=["Front door"], importance="high")
    await hass.async_block_till_done()

    record = loaded_entry.runtime_data.store.async_get(response["id"])
    assert record["labels"] == ["front_door"]
    assert record["channels"] == ["security"]
    assert response["delivered_to"] == ["r1"]
    assert len(notify_calls) == 1
    assert notify_calls[0].data["data"]["tag"] == record["id"]


async def test_live_update_reuses_record(hass: HomeAssistant, loaded_entry, notify_calls):
    first = await _send(hass, live_id="vacuum", message="10%")
    second = await _send(hass, live_id="vacuum", message="50%")
    assert first["id"] == second["id"]
    store = loaded_entry.runtime_data.store
    assert store.async_get(first["id"])["message"] == "50%"
    assert len(store.async_list()) == 1

    await hass.services.async_call(DOMAIN, "end_live_notification", {"live_id": "vacuum"}, blocking=True)
    assert store.async_get(first["id"])["acknowledged"]


async def test_persistent_ack_via_legacy_mobile_action(hass: HomeAssistant, loaded_entry):
    response = await _send(hass, persistent=True)
    store = loaded_entry.runtime_data.store
    assert store.async_list(outstanding_only=True)

    hass.bus.async_fire("mobile_app_notification_action", {"action": f"{LEGACY_ACK_ACTION_PREFIX}{response['id']}"})
    await hass.async_block_till_done()
    assert store.async_get(response["id"])["acknowledged_via"] == "mobile_action"


async def test_legacy_service_alias_and_repair_issue(hass: HomeAssistant, loaded_entry):
    response = await _send(hass, domain=LEGACY_DOMAIN, channel="info")
    assert loaded_entry.runtime_data.store.async_get(response["id"]) is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_service_send_notification")


async def test_clear_history_keeps_outstanding(hass: HomeAssistant, loaded_entry):
    kept = await _send(hass, persistent=True)
    await _send(hass)
    await hass.services.async_call(DOMAIN, "clear_history", {}, blocking=True)
    assert [r["id"] for r in loaded_entry.runtime_data.store.async_list()] == [kept["id"]]


async def test_prune_keeps_outstanding(hass: HomeAssistant, loaded_entry):
    store = loaded_entry.runtime_data.store
    outstanding = await _send(hass, persistent=True)
    for _ in range(3):
        await _send(hass)
    removed = await store.async_prune([], default_max_records=1, default_retention_days=30)
    assert len(removed) == 2
    assert store.async_get(outstanding["id"]) is not None


async def test_websocket_list_and_legacy_prefix(hass: HomeAssistant, loaded_entry, hass_ws_client):
    await _send(hass, channel="security", labels="front_door")
    await _send(hass, channel="security")
    client = await hass_ws_client(hass)

    for prefix in (DOMAIN, LEGACY_DOMAIN):
        await client.send_json_auto_id({"type": f"{prefix}/list", "labels": ["front_door"]})
        result = await client.receive_json()
        assert result["success"], result
        assert len(result["result"]["notifications"]) == 1

    await client.send_json_auto_id({"type": f"{DOMAIN}/config"})
    result = await client.receive_json()
    security = next(c for c in result["result"]["channels"] if c["key"] == "security")
    assert security["labels"] == [{"key": "front_door", "name": "Front door"}]


async def test_restore_config_is_admin_only(hass: HomeAssistant, loaded_entry, hass_read_only_user):
    import pytest
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "restore_config_from_snapshot",
            {"confirm": True},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
