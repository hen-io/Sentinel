"""Compatibility with the pre-rename "K93 ANS" integration (domain k93_ans).

Covers: importing the old config entry (entities/device kept), the deprecated
k93_ans.* services and k93_ans/* websocket commands, and old push action ids.
Data folder/image migration lives in migration.py.

To drop K93 ANS support: delete this module and the call sites marked `# legacy`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import CONF_STORAGE_PATH, DOMAIN
from .options import normalize_options

_LOGGER = logging.getLogger(__name__)

LEGACY_DOMAIN = "k93_ans"
LEGACY_ACK_ACTION_PREFIX = "K93_ACK__"
LEGACY_WS_PREFIX = LEGACY_DOMAIN
LEGACY_STORAGE_DIR_NAME = "K93-Advanced-Notification-System"
LEGACY_IMAGES_DIR_NAME = LEGACY_STORAGE_DIR_NAME

ISSUE_DEPRECATED_SERVICE = "deprecated_service"
ISSUE_LEGACY_FOLDER = "legacy_integration_folder"


@callback
def legacy_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return hass.config_entries.async_entries(LEGACY_DOMAIN)


def legacy_options_for_import(hass: HomeAssistant, legacy_entry: ConfigEntry) -> dict[str, Any]:
    """Old options, normalized; a storage_path pointing at the old default folder is reset."""
    options = normalize_options(legacy_entry.options)
    storage_path = (options.get(CONF_STORAGE_PATH) or "").strip()
    if storage_path:
        resolved = Path(storage_path)
        if not resolved.is_absolute():
            resolved = Path(hass.config.path(storage_path))
        if resolved == Path(hass.config.path(LEGACY_STORAGE_DIR_NAME)):
            options[CONF_STORAGE_PATH] = ""
    return options


async def async_adopt_legacy_entries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unload old K93 ANS entries, move their entities/device to this entry, then remove them."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    for legacy in legacy_entries(hass):
        if legacy.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(legacy.entry_id)

        prefix = f"{legacy.entry_id}_"
        for reg_entry in er.async_entries_for_config_entry(ent_reg, legacy.entry_id):
            new_unique_id = f"{entry.entry_id}_{reg_entry.unique_id.removeprefix(prefix)}"
            if ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id):
                continue
            try:
                ent_reg.async_update_entity_platform(
                    reg_entry.entity_id,
                    DOMAIN,
                    new_config_entry_id=entry.entry_id,
                    new_unique_id=new_unique_id,
                )
            except ValueError:
                _LOGGER.warning("Sentinel could not adopt legacy entity %s", reg_entry.entity_id)

        if device := dev_reg.async_get_device(identifiers={(LEGACY_DOMAIN, legacy.entry_id)}):
            dev_reg.async_update_device(
                device.id,
                new_identifiers={(DOMAIN, entry.entry_id)},
                add_config_entry_id=entry.entry_id,
                remove_config_entry_id=legacy.entry_id,
            )

        await hass.config_entries.async_remove(legacy.entry_id)
        _LOGGER.warning("Sentinel imported and removed the old K93 ANS config entry")


def ack_action_prefixes() -> tuple[str, ...]:
    return (LEGACY_ACK_ACTION_PREFIX,)


def ws_prefixes() -> tuple[str, ...]:
    return (LEGACY_WS_PREFIX,)


def _deprecation_notice(hass: HomeAssistant, service: str) -> Callable[[ServiceCall], None]:
    issue_id = f"{ISSUE_DEPRECATED_SERVICE}_{service}"

    @callback
    def _notice(_call: ServiceCall) -> None:
        if ir.async_get(hass).async_get_issue(DOMAIN, issue_id):
            return
        _LOGGER.warning(
            "%s.%s is deprecated, use %s.%s instead", LEGACY_DOMAIN, service, DOMAIN, service
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DEPRECATED_SERVICE,
            translation_placeholders={
                "old": f"{LEGACY_DOMAIN}.{service}",
                "new": f"{DOMAIN}.{service}",
            },
        )

    return _notice


def async_register_legacy_services(
    hass: HomeAssistant,
    services: Iterable[Any],
    register: Callable[..., None],
) -> None:
    """Register every Sentinel service again under k93_ans.*, with a deprecation notice."""
    for spec in services:
        register(hass, LEGACY_DOMAIN, spec, on_call=_deprecation_notice(hass, spec.name))


@callback
def async_unregister_legacy_services(hass: HomeAssistant, services: Iterable[Any]) -> None:
    for spec in services:
        hass.services.async_remove(LEGACY_DOMAIN, spec.name)


async def async_check_legacy_folder(hass: HomeAssistant) -> None:
    """Raise a repair issue while the old custom_components/k93_ans folder still exists."""
    path = Path(hass.config.path("custom_components", LEGACY_DOMAIN))
    if await hass.async_add_executor_job(path.is_dir):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_LEGACY_FOLDER,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_FOLDER,
            translation_placeholders={"path": str(path)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_FOLDER)
