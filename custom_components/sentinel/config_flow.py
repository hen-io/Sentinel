from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from croniter import croniter
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import legacy
from .const import (
    CONF_CALENDAR_NOTIFICATIONS,
    CONF_CHANNELS,
    CONF_CHAT_HISTORY_MAX_DAYS,
    CONF_CHAT_HISTORY_MAX_MESSAGES,
    CONF_CHAT_REACTION_EMOJI,
    CONF_CHATROOMS,
    CONF_HISTORY_MAX_RECORDS,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_LANGUAGE,
    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    CONF_RECIPIENTS,
    CONF_SCHEDULED_NOTIFICATIONS,
    CONF_STORAGE_PATH,
    DEFAULT_ALL_DAY_TIME,
    DEFAULT_CHANNEL,
    DEFAULT_CHAT_REACTION_EMOJI,
    DEFAULT_QUIET_MIN_IMPORTANCE,
    DOMAIN,
    IMPORTANCE_LEVELS,
    NAME,
)
from .options import channel_labels, default_options, normalize_options
from .routing import normalize_key, normalize_keys

_LOGGER = logging.getLogger(__name__)

ADD_NEW = "__add_new__"
CHANNEL_IMPORTANCE_FIELD_PREFIX = "importance_for_"


class SentinelConfigFlow(ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if legacy.legacy_entries(self.hass):  # legacy
            return await self.async_step_import_legacy()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={}, options=default_options())
        return self.async_show_form(step_id="user")

    async def async_step_import_legacy(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Take over the K93 ANS configuration (entities, history and settings are kept)."""
        if user_input is not None:
            entries = legacy.legacy_entries(self.hass)
            options = legacy.legacy_options_for_import(self.hass, entries[0]) if entries else default_options()
            return self.async_create_entry(title=NAME, data={}, options=options)
        return self.async_show_form(step_id="import_legacy")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SentinelOptionsFlow:
        return SentinelOptionsFlow()


class _FieldError(Exception):

    def __init__(self, field: str, key: str) -> None:
        super().__init__(key)
        self.field = field
        self.key = key


SchemaBuilder = Callable[[dict | None], Awaitable[dict[Any, Any]]]
ItemBuilder = Callable[[dict, dict | None], tuple[dict, bool]]


@dataclass(frozen=True, slots=True)
class _Collection:

    option_key: str
    manage_step: str
    edit_step: str
    select_field: str
    add_label: str


COLLECTIONS: dict[str, _Collection] = {
    "recipient": _Collection(CONF_RECIPIENTS, "manage_recipients", "edit_recipient", "recipient", "Add new recipient"),
    "channel": _Collection(CONF_CHANNELS, "manage_channels", "edit_channel", "channel", "Add new channel"),
    "scheduled": _Collection(
        CONF_SCHEDULED_NOTIFICATIONS, "manage_scheduled", "edit_scheduled", "scheduled", "Add new scheduled notification"
    ),
    "calendar": _Collection(
        CONF_CALENDAR_NOTIFICATIONS,
        "manage_calendar",
        "edit_calendar",
        "calendar_notification",
        "Add new calendar notification",
    ),
    "chatroom": _Collection(CONF_CHATROOMS, "manage_chatrooms", "edit_chatroom", "chatroom", "Add new chatroom"),
}


def _importance_selector() -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS))


def _number_selector(minimum: int) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=minimum, mode="box"))


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _suggested(key: str, existing: dict | None, default: Any = None) -> vol.Optional:
    """Optional field pre-filled with the existing value (clearable)."""
    value = existing.get(key) if existing else None
    return vol.Optional(key, description={"suggested_value": value if value is not None else default})


class SentinelOptionsFlow(OptionsFlow):

    def __init__(self) -> None:
        self._options: dict[str, Any] | None = None
        self._editing_id: str | None = None

    @property
    def options(self) -> dict[str, Any]:
        if self._options is None:
            self._options = normalize_options(self.config_entry.options)
        return self._options

    async def _user_choices(self) -> list[dict[str, str]]:
        users = await self.hass.auth.async_get_users()
        return [{"value": u.id, "label": u.name} for u in users if not u.system_generated]

    def _channel_choices(self) -> list[str]:
        return [c["key"] for c in self.options[CONF_CHANNELS]]

    def _label_choices(self) -> list[dict[str, str]]:
        names = {c["key"]: c["name"] for c in self.options[CONF_CHANNELS]}
        return [
            {"value": key, "label": f"{label['name']} ({names.get(label['channel'], label['channel'])})"}
            for key, label in channel_labels(self.options[CONF_CHANNELS]).items()
        ]

    def _recipient_choices(self) -> list[str]:
        return [r["name"] for r in self.options[CONF_RECIPIENTS]]

    def _save(self) -> None:
        """Persist immediately; the entry reloads and rewrites its config snapshot."""
        self.hass.config_entries.async_update_entry(self.config_entry, options=self.options)

    # Menu

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[*(c.manage_step for c in COLLECTIONS.values()), "advanced", "finish"],
        )

    async def async_step_finish(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.options)

    # Generic collection editing

    async def _manage(self, kind: str, user_input: dict | None) -> ConfigFlowResult:
        collection = COLLECTIONS[kind]
        items = self.options[collection.option_key]
        if user_input is not None or not items:
            choice = (user_input or {}).get(collection.select_field, ADD_NEW)
            self._editing_id = None if choice == ADD_NEW else choice
            return await self._edit(kind, None)
        choices = [{"value": i["id"], "label": i["name"]} for i in items]
        choices.append({"value": ADD_NEW, "label": collection.add_label})
        return self.async_show_form(
            step_id=collection.manage_step,
            data_schema=vol.Schema(
                {
                    vol.Required(collection.select_field): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def _edit(self, kind: str, user_input: dict | None) -> ConfigFlowResult:
        collection = COLLECTIONS[kind]
        items = self.options[collection.option_key]
        existing = next((i for i in items if i["id"] == self._editing_id), None)
        schema_builder: SchemaBuilder = getattr(self, f"_{kind}_schema")
        item_builder: ItemBuilder = getattr(self, f"_{kind}_item")
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                self.options[collection.option_key] = [i for i in items if i["id"] != existing["id"]]
                self._save()
                return await self.async_step_init()
            try:
                item, reopen = item_builder(user_input, existing)
            except _FieldError as err:
                errors[err.field] = err.key
            except Exception:
                _LOGGER.exception("Sentinel: failed saving %s", kind)
                errors["base"] = "unknown"
            else:
                if existing:
                    self.options[collection.option_key] = [item if i["id"] == item["id"] else i for i in items]
                else:
                    self.options[collection.option_key] = [*items, item]
                self._save()
                if reopen:
                    self._editing_id = item["id"]
                    return await self._edit(kind, None)
                return await self.async_step_init()

        schema = vol.Schema(await schema_builder(existing))
        if existing:
            schema = schema.extend({vol.Optional("remove", default=False): selector.BooleanSelector()})
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id=collection.edit_step, data_schema=schema, errors=errors)

    async def async_step_manage_recipients(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._manage("recipient", user_input)

    async def async_step_edit_recipient(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._edit("recipient", user_input)

    async def async_step_manage_channels(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._manage("channel", user_input)

    async def async_step_edit_channel(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._edit("channel", user_input)

    async def async_step_manage_scheduled(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._manage("scheduled", user_input)

    async def async_step_edit_scheduled(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._edit("scheduled", user_input)

    async def async_step_manage_calendar(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._manage("calendar", user_input)

    async def async_step_edit_calendar(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._edit("calendar", user_input)

    async def async_step_manage_chatrooms(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._manage("chatroom", user_input)

    async def async_step_edit_chatroom(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._edit("chatroom", user_input)

    # Recipients

    async def _recipient_schema(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        notify_services = sorted(self.hass.services.async_services().get("notify", {}))
        users = [{"value": "", "label": "(not linked)"}, *await self._user_choices()]
        schema: dict[Any, Any] = {
            vol.Required("name", default=e.get("name", "")): selector.TextSelector(),
            vol.Required("notify_service", default=e.get("notify_service", vol.UNDEFINED)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=notify_services, custom_value=True, mode="dropdown")
            ),
            _suggested("person_entity_id", existing): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person")
            ),
            _suggested("interactive_entity_id", existing): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional("linked_user_id", default=e.get("linked_user_id") or ""): selector.SelectSelector(
                selector.SelectSelectorConfig(options=users, mode="dropdown")
            ),
            vol.Optional("min_importance", default=e.get("min_importance", "normal")): _importance_selector(),
            vol.Optional("allowed_channels", default=e.get("allowed_channels", [])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._channel_choices(), multiple=True, custom_value=True)
            ),
            _suggested("quiet_start", existing): selector.TimeSelector(),
            _suggested("quiet_end", existing): selector.TimeSelector(),
            vol.Optional(
                "quiet_min_importance", default=e.get("quiet_min_importance", DEFAULT_QUIET_MIN_IMPORTANCE)
            ): _importance_selector(),
            vol.Optional("enabled", default=e.get("enabled", True)): selector.BooleanSelector(),
        }
        overrides = [{"value": "", "label": "(use recipient default)"}, *IMPORTANCE_LEVELS]
        channel_importance = e.get("channel_importance", {})
        for channel_key in e.get("allowed_channels", []):
            field = vol.Optional(
                f"{CHANNEL_IMPORTANCE_FIELD_PREFIX}{channel_key}", default=channel_importance.get(channel_key, "")
            )
            schema[field] = selector.SelectSelector(selector.SelectSelectorConfig(options=overrides))
        return schema

    def _recipient_item(self, user_input: dict, existing: dict | None) -> tuple[dict, bool]:
        allowed = user_input.get("allowed_channels", [])
        item = {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "name": user_input["name"],
            "notify_service": user_input["notify_service"],
            "person_entity_id": user_input.get("person_entity_id") or None,
            "interactive_entity_id": user_input.get("interactive_entity_id") or None,
            "linked_user_id": user_input.get("linked_user_id") or None,
            "min_importance": user_input["min_importance"],
            "allowed_channels": allowed,
            "channel_importance": {
                key.removeprefix(CHANNEL_IMPORTANCE_FIELD_PREFIX): value
                for key, value in user_input.items()
                if key.startswith(CHANNEL_IMPORTANCE_FIELD_PREFIX)
                and value
                and key.removeprefix(CHANNEL_IMPORTANCE_FIELD_PREFIX) in allowed
            },
            "quiet_start": user_input.get("quiet_start") or None,
            "quiet_end": user_input.get("quiet_end") or None,
            "quiet_min_importance": user_input.get("quiet_min_importance", DEFAULT_QUIET_MIN_IMPORTANCE),
            "enabled": user_input["enabled"],
        }
        # Reopen so per-channel importance fields appear for newly allowed channels.
        reopen = set(allowed) != set((existing or {}).get("allowed_channels", []))
        return item, reopen

    # Channels

    async def _channel_schema(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        label_names = [label["name"] for label in e.get("labels", [])]
        return {
            vol.Required("key", default=e.get("key", "")): selector.TextSelector(),
            vol.Required("name", default=e.get("name", "")): selector.TextSelector(),
            vol.Optional("min_importance", default=e.get("min_importance", "low")): _importance_selector(),
            vol.Optional("enabled", default=e.get("enabled", True)): selector.BooleanSelector(),
            _suggested("color", existing, ""): selector.TextSelector(),
            vol.Optional("labels", default=label_names): selector.SelectSelector(
                selector.SelectSelectorConfig(options=label_names, multiple=True, custom_value=True)
            ),
            _suggested("retention_days", existing): _number_selector(1),
            _suggested("max_records", existing): _number_selector(1),
        }

    def _channel_item(self, user_input: dict, existing: dict | None) -> tuple[dict, bool]:
        key = normalize_key(user_input["key"])
        if not key:
            raise _FieldError("key", "invalid_key")
        if any(c["key"] == key and (existing is None or c["id"] != existing["id"]) for c in self.options[CONF_CHANNELS]):
            raise _FieldError("key", "duplicate_key")
        labels: list[dict[str, str]] = []
        for name in user_input.get("labels", []):
            label_key = normalize_key(name)
            if label_key and all(label["key"] != label_key for label in labels):
                labels.append({"key": label_key, "name": name.strip()})
        item = {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "key": key,
            "name": user_input["name"],
            "min_importance": user_input["min_importance"],
            "enabled": user_input["enabled"],
            "color": user_input.get("color") or None,
            "retention_days": _optional_int(user_input.get("retention_days")),
            "max_records": _optional_int(user_input.get("max_records")),
            "labels": labels,
        }
        return item, False

    # Scheduled and calendar notifications

    def _trigger_fields(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        return {
            _suggested("icon", existing, ""): selector.TextSelector(),
            vol.Optional("channel", default=e.get("channel", DEFAULT_CHANNEL)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._channel_choices(), custom_value=True)
            ),
            vol.Optional("labels", default=e.get("labels", [])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._label_choices(), multiple=True, custom_value=True)
            ),
            vol.Optional("importance", default=e.get("importance", "normal")): _importance_selector(),
            vol.Optional("persistent", default=e.get("persistent", False)): selector.BooleanSelector(),
            vol.Optional("home_only", default=e.get("home_only", False)): selector.BooleanSelector(),
            vol.Optional("target_recipients", default=e.get("target_recipients", [])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._recipient_choices(), multiple=True, custom_value=True)
            ),
        }

    @staticmethod
    def _trigger_item(user_input: dict, existing: dict | None) -> dict[str, Any]:
        return {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "name": user_input["name"],
            "enabled": user_input["enabled"],
            "icon": user_input.get("icon") or None,
            "channel": normalize_key(user_input.get("channel")) or DEFAULT_CHANNEL,
            "labels": normalize_keys(user_input.get("labels")),
            "importance": user_input["importance"],
            "persistent": user_input["persistent"],
            "home_only": user_input["home_only"],
            "target_recipients": user_input.get("target_recipients", []),
        }

    async def _scheduled_schema(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        return {
            vol.Required("name", default=e.get("name", "")): selector.TextSelector(),
            vol.Required("cron", default=e.get("cron", "0 8 * * *")): selector.TextSelector(),
            vol.Optional("enabled", default=e.get("enabled", True)): selector.BooleanSelector(),
            vol.Required("title", default=e.get("title", "")): selector.TextSelector(),
            vol.Required("message", default=e.get("message", "")): selector.TextSelector(),
            **self._trigger_fields(existing),
        }

    def _scheduled_item(self, user_input: dict, existing: dict | None) -> tuple[dict, bool]:
        cron = user_input["cron"].strip()
        if not croniter.is_valid(cron):
            raise _FieldError("cron", "invalid_cron")
        item = {
            **self._trigger_item(user_input, existing),
            "cron": cron,
            "title": user_input["title"],
            "message": user_input["message"],
        }
        return item, False

    async def _calendar_schema(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        return {
            vol.Required("name", default=e.get("name", "")): selector.TextSelector(),
            vol.Required("calendar_entity", default=e.get("calendar_entity", vol.UNDEFINED)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar")
            ),
            vol.Optional("enabled", default=e.get("enabled", True)): selector.BooleanSelector(),
            vol.Optional("all_day_time", default=e.get("all_day_time", DEFAULT_ALL_DAY_TIME)): selector.TimeSelector(),
            _suggested("title", existing, ""): selector.TextSelector(),
            _suggested("message", existing, ""): selector.TextSelector(),
            **self._trigger_fields(existing),
        }

    def _calendar_item(self, user_input: dict, existing: dict | None) -> tuple[dict, bool]:
        item = {
            **self._trigger_item(user_input, existing),
            "calendar_entity": user_input["calendar_entity"],
            "all_day_time": user_input.get("all_day_time") or DEFAULT_ALL_DAY_TIME,
            "title": user_input.get("title") or None,
            "message": user_input.get("message") or None,
        }
        return item, False

    # Chatrooms

    async def _chatroom_schema(self, existing: dict | None) -> dict[Any, Any]:
        e = existing or {}
        return {
            vol.Required("name", default=e.get("name", "")): selector.TextSelector(),
            _suggested("icon", existing, ""): selector.TextSelector(),
            vol.Optional("enabled", default=e.get("enabled", True)): selector.BooleanSelector(),
            vol.Optional("access_mode", default=e.get("access_mode", "all")): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "all", "label": "All users"},
                        {"value": "selected", "label": "Selected users"},
                    ]
                )
            ),
            vol.Optional("allowed_user_ids", default=e.get("allowed_user_ids", [])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=await self._user_choices(), multiple=True)
            ),
            _suggested("history_max_days", existing): _number_selector(1),
            _suggested("history_max_messages", existing): _number_selector(1),
            vol.Optional("new_message_alert", default=e.get("new_message_alert", False)): selector.BooleanSelector(),
            _suggested("navigate_url", existing, ""): selector.TextSelector(),
        }

    def _chatroom_item(self, user_input: dict, existing: dict | None) -> tuple[dict, bool]:
        item = {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "name": user_input["name"],
            "icon": user_input.get("icon") or None,
            "enabled": user_input["enabled"],
            "access_mode": user_input["access_mode"],
            "allowed_user_ids": user_input.get("allowed_user_ids", []),
            "history_max_days": _optional_int(user_input.get("history_max_days")),
            "history_max_messages": _optional_int(user_input.get("history_max_messages")),
            "new_message_alert": user_input["new_message_alert"],
            "navigate_url": user_input.get("navigate_url") or None,
        }
        return item, False

    # Advanced

    async def async_step_advanced(self, user_input: dict | None = None) -> ConfigFlowResult:
        options = self.options
        if user_input is not None:
            for key in (
                CONF_HISTORY_RETENTION_DAYS,
                CONF_HISTORY_MAX_RECORDS,
                CONF_CHAT_HISTORY_MAX_DAYS,
                CONF_CHAT_HISTORY_MAX_MESSAGES,
                CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
            ):
                options[key] = int(user_input[key])
            options[CONF_CHAT_REACTION_EMOJI] = user_input.get(CONF_CHAT_REACTION_EMOJI, [])
            options[CONF_LANGUAGE] = user_input[CONF_LANGUAGE]
            options[CONF_STORAGE_PATH] = (user_input.get(CONF_STORAGE_PATH) or "").strip()
            self._save()
            return await self.async_step_init()

        schema = {
            vol.Optional(CONF_HISTORY_RETENTION_DAYS, default=options[CONF_HISTORY_RETENTION_DAYS]): _number_selector(1),
            vol.Optional(CONF_HISTORY_MAX_RECORDS, default=options[CONF_HISTORY_MAX_RECORDS]): _number_selector(1),
            vol.Optional(CONF_CHAT_HISTORY_MAX_DAYS, default=options[CONF_CHAT_HISTORY_MAX_DAYS]): _number_selector(1),
            vol.Optional(
                CONF_CHAT_HISTORY_MAX_MESSAGES, default=options[CONF_CHAT_HISTORY_MAX_MESSAGES]
            ): _number_selector(1),
            vol.Optional(CONF_CHAT_REACTION_EMOJI, default=options[CONF_CHAT_REACTION_EMOJI]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=DEFAULT_CHAT_REACTION_EMOJI, multiple=True, custom_value=True)
            ),
            vol.Optional(CONF_LANGUAGE, default=options[CONF_LANGUAGE]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "auto", "label": "Automatic (match Home Assistant)"},
                        {"value": "en", "label": "English"},
                        {"value": "no", "label": "Norsk"},
                    ]
                )
            ),
            vol.Optional(
                CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES, default=options[CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES]
            ): _number_selector(0),
            vol.Optional(
                CONF_STORAGE_PATH, description={"suggested_value": options.get(CONF_STORAGE_PATH, "")}
            ): selector.TextSelector(),
        }
        return self.async_show_form(step_id="advanced", data_schema=vol.Schema(schema))
