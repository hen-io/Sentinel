"""Shared fixtures. Run: pip install -r requirements_test.txt && pytest"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.sentinel.const import DOMAIN
from custom_components.sentinel.options import default_options

pytest_plugins = "pytest_homeassistant_custom_component"

NOTIFY_SERVICE = "mobile_app_phone"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def notify_calls(hass: HomeAssistant):
    return async_mock_service(hass, "notify", NOTIFY_SERVICE)


def make_options(**overrides) -> dict:
    options = default_options()
    for channel in options["channels"]:
        if channel["key"] == "security":
            channel["labels"] = [{"key": "front_door", "name": "Front door"}]
    options["recipients"] = [
        {
            "id": "r1",
            "name": "Phone",
            "notify_service": NOTIFY_SERVICE,
            "min_importance": "low",
            "allowed_channels": [],
            "channel_importance": {},
            "enabled": True,
        }
    ]
    options.update(overrides)
    return options


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, notify_calls) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=make_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
