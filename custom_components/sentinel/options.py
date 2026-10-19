"""Config entry option defaults and normalization."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .const import (
    BUILTIN_CHANNELS,
    CHAT_CHANNEL,
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
    DEFAULT_CHAT_HISTORY_MAX_DAYS,
    DEFAULT_CHAT_HISTORY_MAX_MESSAGES,
    DEFAULT_CHAT_REACTION_EMOJI,
    DEFAULT_HISTORY_MAX_RECORDS,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_LANGUAGE,
    DEFAULT_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    DEFAULT_QUIET_MIN_IMPORTANCE,
    DEFAULT_STORAGE_PATH,
)
from .models import Channel


def new_channel(key: str, name: str, min_importance: str) -> Channel:
    return {
        "id": str(uuid.uuid4()),
        "key": key,
        "name": name,
        "min_importance": min_importance,
        "enabled": True,
        "color": None,
        "retention_days": None,
        "max_records": None,
        "labels": [],
    }


def default_options() -> dict[str, Any]:
    return {
        CONF_RECIPIENTS: [],
        CONF_SCHEDULED_NOTIFICATIONS: [],
        CONF_CALENDAR_NOTIFICATIONS: [],
        CONF_CHATROOMS: [],
        CONF_CHANNELS: [new_channel(c["key"], c["name"], c["min_importance"]) for c in BUILTIN_CHANNELS],
        CONF_HISTORY_RETENTION_DAYS: DEFAULT_HISTORY_RETENTION_DAYS,
        CONF_HISTORY_MAX_RECORDS: DEFAULT_HISTORY_MAX_RECORDS,
        CONF_CHAT_HISTORY_MAX_DAYS: DEFAULT_CHAT_HISTORY_MAX_DAYS,
        CONF_CHAT_HISTORY_MAX_MESSAGES: DEFAULT_CHAT_HISTORY_MAX_MESSAGES,
        CONF_CHAT_REACTION_EMOJI: list(DEFAULT_CHAT_REACTION_EMOJI),
        CONF_LANGUAGE: DEFAULT_LANGUAGE,
        CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES: DEFAULT_LIVE_INACTIVITY_TIMEOUT_MINUTES,
        CONF_STORAGE_PATH: DEFAULT_STORAGE_PATH,
    }


_CHANNEL_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "color": None,
    "retention_days": None,
    "max_records": None,
    "labels": [],
}
_RECIPIENT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "allowed_channels": [],
    "channel_importance": {},
    "person_entity_id": None,
    "interactive_entity_id": None,
    "linked_user_id": None,
    "quiet_start": None,
    "quiet_end": None,
    "quiet_min_importance": DEFAULT_QUIET_MIN_IMPORTANCE,
}
_TRIGGER_DEFAULTS: dict[str, Any] = {"labels": []}


def _with_defaults(items: list[dict[str, Any]], defaults: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**defaults, **item} for item in items]


def normalize_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Fill missing keys with defaults without changing any user choice."""
    defaults = default_options()
    normalized = {**defaults, **options}
    channels = _with_defaults(list(normalized[CONF_CHANNELS]), _CHANNEL_DEFAULTS)
    if not any(c.get("key") == CHAT_CHANNEL for c in channels):
        chat = next(c for c in BUILTIN_CHANNELS if c["key"] == CHAT_CHANNEL)
        channels.append(new_channel(chat["key"], chat["name"], chat["min_importance"]))
    normalized[CONF_CHANNELS] = channels
    normalized[CONF_RECIPIENTS] = _with_defaults(normalized[CONF_RECIPIENTS], _RECIPIENT_DEFAULTS)
    for key in (CONF_SCHEDULED_NOTIFICATIONS, CONF_CALENDAR_NOTIFICATIONS):
        normalized[key] = _with_defaults(normalized[key], _TRIGGER_DEFAULTS)
    return normalized


def channel_labels(channels: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """All configured labels keyed by label key (first channel wins on duplicates)."""
    labels: dict[str, dict[str, str]] = {}
    for channel in channels:
        for label in channel.get("labels") or []:
            labels.setdefault(label["key"], {**label, "channel": channel["key"]})
    return labels
