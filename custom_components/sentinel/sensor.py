from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_CHANNELS, CONF_CHATROOMS, CONF_RECIPIENTS, DOMAIN, NAME, SIGNAL_DELETED, SIGNAL_UPDATED
from .runtime import SentinelConfigEntry, SentinelData


def _local_dates(runtime: SentinelData) -> list[date]:
    return [
        dt_util.as_local(created).date()
        for r in runtime.store.async_list()
        if r.get("show_in_history", True) and (created := dt_util.parse_datetime(r["created"]))
    ]


def _sent_since(runtime: SentinelData, start: Callable[[date], date]) -> int:
    today = dt_util.now().date()
    first = start(today)
    return sum(1 for d in _local_dates(runtime) if d >= first)


def _channels_attrs(runtime: SentinelData) -> dict[str, Any]:
    return {
        "channels": [
            {
                "key": c["key"],
                "name": c["name"],
                "min_importance": c["min_importance"],
                "enabled": c.get("enabled", True),
                "color": c.get("color"),
                "labels": [{"key": l["key"], "name": l["name"]} for l in c.get("labels") or []],
            }
            for c in runtime.options.get(CONF_CHANNELS, [])
        ]
    }


def _recipients_attrs(runtime: SentinelData) -> dict[str, Any]:
    return {
        "recipients": [
            {
                "name": r["name"],
                "notify_service": r["notify_service"],
                "min_importance": r["min_importance"],
                "allowed_channels": r.get("allowed_channels") or [],
                "person_entity_id": r.get("person_entity_id"),
                "enabled": r.get("enabled", True),
            }
            for r in runtime.options.get(CONF_RECIPIENTS, [])
        ]
    }


def _chatrooms_attrs(runtime: SentinelData) -> dict[str, Any]:
    return {
        "chatrooms": [
            {"id": c["id"], "name": c["name"], "icon": c.get("icon"), "enabled": c.get("enabled", True)}
            for c in runtime.options.get(CONF_CHATROOMS, [])
        ]
    }


@dataclass(frozen=True, kw_only=True)
class SentinelSensorDescription(SensorEntityDescription):

    value_fn: Callable[[SentinelData], int]
    attrs_fn: Callable[[SentinelData], dict[str, Any]] | None = None


SENSORS: tuple[SentinelSensorDescription, ...] = (
    SentinelSensorDescription(
        key="channels",
        name="Configured channels",
        icon="mdi:tune-vertical",
        value_fn=lambda rt: len(rt.options.get(CONF_CHANNELS, [])),
        attrs_fn=_channels_attrs,
    ),
    SentinelSensorDescription(
        key="recipients",
        name="Configured recipients",
        icon="mdi:account-multiple",
        value_fn=lambda rt: len(rt.options.get(CONF_RECIPIENTS, [])),
        attrs_fn=_recipients_attrs,
    ),
    SentinelSensorDescription(
        key="chatrooms",
        name="Configured chatrooms",
        icon="mdi:forum",
        value_fn=lambda rt: len(rt.options.get(CONF_CHATROOMS, [])),
        attrs_fn=_chatrooms_attrs,
    ),
    SentinelSensorDescription(
        key="stored",
        name="Notifications stored",
        icon="mdi:database",
        value_fn=lambda rt: len(_local_dates(rt)),
    ),
    SentinelSensorDescription(
        key="sent_today",
        name="Sent today",
        icon="mdi:calendar-today",
        value_fn=lambda rt: _sent_since(rt, lambda today: today),
    ),
    SentinelSensorDescription(
        key="sent_this_week",
        name="Sent this week",
        icon="mdi:calendar-week",
        value_fn=lambda rt: _sent_since(rt, lambda today: today - timedelta(days=today.weekday())),
    ),
    SentinelSensorDescription(
        key="sent_this_month",
        name="Sent this month",
        icon="mdi:calendar-month",
        value_fn=lambda rt: _sent_since(rt, lambda today: today.replace(day=1)),
    ),
    SentinelSensorDescription(
        key="unacknowledged",
        name="Unacknowledged notifications",
        icon="mdi:bell-alert",
        value_fn=lambda rt: len(rt.store.async_list(outstanding_only=True)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SentinelConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [
            *(SentinelSensor(entry, description) for description in SENSORS),
            SentinelDatabaseSizeSensor(entry),
        ]
    )


class SentinelSensorBase(SensorEntity):

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: SentinelConfigEntry, key: str) -> None:
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="@hen-io | Henrik Isefjær Olsen",
            model="Notification system",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_change(*_args: Any) -> None:
            self.async_schedule_update_ha_state(True)

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, _on_change))
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_DELETED, _on_change))


class SentinelSensor(SentinelSensorBase):

    entity_description: SentinelSensorDescription

    def __init__(self, entry: SentinelConfigEntry, description: SentinelSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int:
        return self.entity_description.value_fn(self._runtime)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self._runtime) if attrs_fn else None


class SentinelDatabaseSizeSensor(SentinelSensorBase):

    _attr_name = "Database size"
    _attr_icon = "mdi:database-outline"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.KILOBYTES

    def __init__(self, entry: SentinelConfigEntry) -> None:
        super().__init__(entry, "database_size")

    async def async_update(self) -> None:
        self._attr_native_value = await self.hass.async_add_executor_job(self._runtime.db.size_bytes)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()
        self.async_write_ha_state()
