"""K93 ANS -> Sentinel upgrade: config entry, entities, data folder and images."""

from __future__ import annotations

import json
import sqlite3

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sentinel.const import DOMAIN
from custom_components.sentinel.legacy import LEGACY_DOMAIN, LEGACY_STORAGE_DIR_NAME

from .conftest import make_options

OLD_IMAGE_URL = f"/local/{LEGACY_STORAGE_DIR_NAME}/security/n1_1.jpg"


def _write_legacy_data(config_dir) -> None:
    data_dir = config_dir / LEGACY_STORAGE_DIR_NAME
    data_dir.mkdir()
    record = {
        "id": "n1",
        "created": "2026-10-01T10:00:00+00:00",
        "live_id": None,
        "title": "Old",
        "message": "From K93",
        "channel": "security",
        "channels": ["security"],
        "importance": "high",
        "image": OLD_IMAGE_URL,
        "image_managed": True,
        "requires_ack": False,
        "acknowledged": False,
        "show_in_history": True,
        "recipients": {},
    }
    conn = sqlite3.connect(data_dir / "database.db")
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, live_id TEXT, channel TEXT NOT NULL, "
        "created TEXT NOT NULL, acknowledged INTEGER NOT NULL, record_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?)",
        ("n1", None, "security", record["created"], 0, json.dumps(record)),
    )
    conn.commit()
    conn.close()
    image = config_dir / "www" / LEGACY_STORAGE_DIR_NAME / "security" / "n1_1.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff old")


async def test_import_from_k93_ans(hass: HomeAssistant, isolated_config_dir, notify_calls):
    _write_legacy_data(isolated_config_dir)
    legacy_entry = MockConfigEntry(domain=LEGACY_DOMAIN, unique_id=LEGACY_DOMAIN, options=make_options())
    legacy_entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "sensor",
        LEGACY_DOMAIN,
        f"{legacy_entry.entry_id}_unacknowledged",
        config_entry=legacy_entry,
        suggested_object_id="k93_ans_unacknowledged_notifications",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_legacy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options["recipients"][0]["id"] == "r1"
    assert not hass.config_entries.async_entries(LEGACY_DOMAIN)

    adopted = ent_reg.async_get(old.entity_id)
    assert adopted.platform == DOMAIN
    assert adopted.unique_id == f"{entry.entry_id}_unacknowledged"
    assert hass.states.get(old.entity_id) is not None

    assert (isolated_config_dir / "sentinel" / "database.db").is_file()
    assert (isolated_config_dir / LEGACY_STORAGE_DIR_NAME / "database.db").is_file(), "backup kept"
    record = entry.runtime_data.store.async_get("n1")
    assert record["labels"] == []
    assert record["image"] == "/local/sentinel/notifications/security/n1_1.jpg"
    assert (isolated_config_dir / "www" / "sentinel" / "notifications" / "security" / "n1_1.jpg").is_file()

    # Old service names keep working after the import.
    assert hass.services.has_service(LEGACY_DOMAIN, "send_notification")


async def test_fresh_install_without_legacy(hass: HomeAssistant, isolated_config_dir):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert (isolated_config_dir / "sentinel" / "database.db").is_file()
    assert not (isolated_config_dir / LEGACY_STORAGE_DIR_NAME).exists()


async def test_storage_failure_keeps_k93_ans(hass: HomeAssistant, isolated_config_dir):
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState

    legacy_entry = MockConfigEntry(domain=LEGACY_DOMAIN, unique_id=LEGACY_DOMAIN, options=make_options())
    legacy_entry.add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=make_options())
    entry.add_to_hass(hass)
    with patch("custom_components.sentinel.Database.open", side_effect=OSError("disk full")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.config_entries.async_entries(LEGACY_DOMAIN), "old entry is kept until Sentinel takes over"
