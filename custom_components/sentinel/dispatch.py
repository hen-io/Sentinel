"""Notification pipeline: build record, route to recipients, persist, acknowledge, delete."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
    UpdateType,
)
from homeassistant.const import STATE_HOME, STATE_OFF
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ACK_ACTION_LABELS,
    ACK_ACTION_PREFIX,
    ANDROID_IMPORTANCE_MAP,
    CHAT_ALERT_LIVE_ID_PREFIX,
    CONF_CHANNELS,
    CONF_LANGUAGE,
    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    CONF_RECIPIENTS,
    DEFAULT_CHANNEL,
    DEFAULT_LANGUAGE,
    EVENT_NOTIFICATION,
    IOS_INTERRUPTION_MAP,
    MAX_ACTIONS,
    SIGNAL_DELETED,
    SIGNAL_UPDATED,
)
from .images import async_capture_entity_image
from .models import NotificationRecord
from .options import channel_labels
from .routing import is_outstanding, normalize_keys, recipient_matches, recipient_targeted
from .runtime import SentinelData

_LOGGER = logging.getLogger(__name__)

_FALLBACK_CHANNEL = {
    "key": DEFAULT_CHANNEL,
    "name": DEFAULT_CHANNEL,
    "min_importance": "low",
    "enabled": True,
}


def resolve_language(hass: HomeAssistant, options: dict[str, Any]) -> str:
    configured = options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    if configured and configured != "auto":
        return configured
    return "no" if (hass.config.language or "").lower().startswith(("nb", "nn", "no")) else "en"


def build_record(
    data: dict[str, Any], ack_label: str, existing: NotificationRecord | None
) -> NotificationRecord:
    """Record from validated send_notification data; reuses id/created of a live record."""
    notification_id = existing["id"] if existing else str(uuid.uuid4())
    channels = normalize_keys(data.get("channel")) or [DEFAULT_CHANNEL]
    persistent = data.get("persistent", False)
    actions = list(data.get("actions") or [])
    if persistent and not any(a["action"].startswith(ACK_ACTION_PREFIX) for a in actions):
        actions = [
            *actions[: MAX_ACTIONS - 1],
            {"action": f"{ACK_ACTION_PREFIX}{notification_id}", "title": ack_label},
        ]
    return {
        "id": notification_id,
        "created": existing["created"] if existing else dt_util.utcnow().isoformat(),
        "updated_at": dt_util.utcnow().isoformat() if existing else None,
        "live_id": data.get("live_id"),
        "title": data["title"],
        "message": data["message"],
        "icon": data.get("icon"),
        "image": data.get("image"),
        "image_managed": False,
        "channel": channels[0],
        "channels": channels,
        "labels": normalize_keys(data.get("labels")),
        "importance": data.get("importance", "normal"),
        "actions": actions[:MAX_ACTIONS],
        "persistent": persistent,
        "data": dict(data.get("data") or {}),
        "recipients": {},
        "requires_ack": persistent,
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_via": None,
        "dismissed": False,
        "source": data.get("source"),
        "target_recipients": data.get("target_recipients") or None,
        "home_only": data.get("home_only", False),
        "interactive_only": data.get("interactive_only", False),
        "show_in_history": data.get("show_in_history", True),
        "dismiss_on_action": data.get("dismiss_on_action", False),
        "clear_on_acknowledge": data.get("clear_on_acknowledge", True),
    }


def _is_mobile_app_target(notify_service: str) -> bool:
    return notify_service.startswith("mobile_app_")


def _is_home(hass: HomeAssistant, recipient: dict[str, Any]) -> bool:
    person_entity_id = recipient.get("person_entity_id")
    if not person_entity_id:
        return True
    state = hass.states.get(person_entity_id)
    return state is not None and state.state == STATE_HOME


def _is_inactive(hass: HomeAssistant, recipient: dict[str, Any], timeout: timedelta | None) -> bool:
    """Interactive sensor has been off for at least the configured timeout."""
    entity_id = recipient.get("interactive_entity_id")
    if timeout is None or not entity_id:
        return False
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_OFF and dt_util.utcnow() - state.last_changed >= timeout


def _inactivity_timeout(options: dict[str, Any]) -> timedelta | None:
    minutes = options.get(CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES) or 0
    return timedelta(minutes=minutes) if minutes > 0 else None


def _resolve_image(record: NotificationRecord) -> str | None:
    if record.get("image"):
        return record["image"]
    icon = record.get("icon")
    return icon if icon and not icon.startswith("mdi:") else None


def _create_persistent_notification(hass: HomeAssistant, record: NotificationRecord) -> None:
    message = record["message"]
    if image := _resolve_image(record):
        message = f"{message}\n\n![]({image})"
    persistent_notification.async_create(hass, message, title=record["title"], notification_id=record["id"])


def _build_notify_payload(record: NotificationRecord) -> dict[str, Any]:
    return {"title": record["title"], "message": record["message"], "data": dict(record["data"])}


def _build_mobile_app_payload(record: NotificationRecord, channel_name: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tag": record["id"],
        "channel": channel_name,
        "importance": ANDROID_IMPORTANCE_MAP.get(record["importance"], "default"),
        "push": {"interruption-level": IOS_INTERRUPTION_MAP.get(record["importance"], "active")},
    }
    if record.get("actions"):
        data["actions"] = record["actions"]
        if not record.get("dismiss_on_action"):
            data["sticky"] = True
    if record.get("persistent"):
        data["sticky"] = True
        data["persistent"] = True
    icon = record.get("icon")
    if icon and icon.startswith("mdi:"):
        data["notification_icon"] = icon
    if image := _resolve_image(record):
        data["image"] = image
    data.update(record["data"])
    return {"title": record["title"], "message": record["message"], "data": data}


def _resolve_channels(options: dict[str, Any], record: NotificationRecord) -> list[dict[str, Any]]:
    defs = {c["key"]: c for c in options.get(CONF_CHANNELS, [])}
    resolved = []
    for key in record["channels"]:
        channel = defs.get(key)
        if channel is None:
            if key != DEFAULT_CHANNEL:
                _LOGGER.warning("Sentinel: unknown channel '%s', using default routing", key)
            channel = _FALLBACK_CHANNEL
        resolved.append(channel)
    if record["labels"]:
        known = channel_labels([c for c in resolved if c is not _FALLBACK_CHANNEL])
        if unknown := [label for label in record["labels"] if label not in known]:
            _LOGGER.debug("Sentinel: labels %s are not defined on channels %s", unknown, record["channels"])
    return resolved


async def _async_call_notify(hass: HomeAssistant, service: str, payload: dict[str, Any]) -> str | None:
    """Send one push; returns an error string or None."""
    try:
        await hass.services.async_call("notify", service, payload, blocking=False)
    except ServiceNotFound as err:
        _LOGGER.warning("Sentinel could not deliver to notify.%s: %s", service, err)
        return str(err)
    except Exception as err:
        _LOGGER.exception("Sentinel failed delivering to notify.%s", service)
        return str(err)
    return None


async def _async_deliver(
    hass: HomeAssistant, runtime: SentinelData, record: NotificationRecord, previous: NotificationRecord | None
) -> None:
    options = runtime.options
    channels = _resolve_channels(options, record)
    previous_deliveries: dict[str, Any] = (previous or {}).get("recipients") or {}
    timeout = _inactivity_timeout(options)
    now = dt_util.now().time()
    targets = record.get("target_recipients")

    deliveries: dict[str, Any] = {}
    for recipient in options.get(CONF_RECIPIENTS, []):
        if not recipient.get("enabled", True):
            continue
        if targets and not recipient_targeted(recipient, targets):
            continue
        matched = recipient_matches(
            recipient,
            record,
            channels,
            is_home=_is_home(hass, recipient),
            is_inactive=_is_inactive(hass, recipient, timeout),
            now=now,
        )
        prior = previous_deliveries.get(recipient["id"]) or {}
        delivery = {
            "notify_service": recipient["notify_service"],
            "matched": matched,
            "dispatched": bool(prior.get("dispatched")),
            "dispatch_error": None,
            "dispatched_at": prior.get("dispatched_at"),
        }
        if matched:
            service = recipient["notify_service"]
            payload = (
                _build_mobile_app_payload(record, channels[0]["name"])
                if _is_mobile_app_target(service)
                else _build_notify_payload(record)
            )
            if error := await _async_call_notify(hass, service, payload):
                delivery["dispatch_error"] = error
            else:
                delivery["dispatched"] = True
                delivery["dispatched_at"] = dt_util.utcnow().isoformat()
        deliveries[recipient["id"]] = delivery

    for recipient_id, prior_delivery in previous_deliveries.items():
        deliveries.setdefault(recipient_id, prior_delivery)
    record["recipients"] = deliveries


async def async_send_notification(
    hass: HomeAssistant, runtime: SentinelData, data: dict[str, Any]
) -> NotificationRecord:
    """Create or update (live_id) a notification and deliver it."""
    live_id = data.get("live_id")
    lock = runtime.live_lock(live_id) if live_id else contextlib.nullcontext()
    async with lock:
        store = runtime.store
        existing = store.async_get_by_live_id(live_id) if live_id else None
        language = resolve_language(hass, runtime.options)
        record = build_record(data, ACK_ACTION_LABELS.get(language, ACK_ACTION_LABELS["en"]), existing)
        if (image_entity := data.get("image_entity")) and not record["image"]:
            await async_capture_entity_image(hass, record, image_entity)
        await _async_deliver(hass, runtime, record, existing)
        if record["persistent"]:
            _create_persistent_notification(hass, record)
        await store.async_add(record)

    async_dispatcher_send(hass, SIGNAL_UPDATED, record)
    hass.bus.async_fire(
        EVENT_NOTIFICATION,
        {
            key: record[key]
            for key in ("id", "live_id", "title", "message", "channel", "channels", "labels", "importance", "source")
        },
    )
    return record


def async_restore_persistent_notifications(hass: HomeAssistant, runtime: SentinelData) -> None:
    for record in runtime.store.async_list(outstanding_only=True):
        if record.get("persistent"):
            _create_persistent_notification(hass, record)


async def async_end_live_notifications(hass: HomeAssistant, runtime: SentinelData) -> None:
    """End live notifications left over from before a restart (chat alerts excluded)."""
    for record in list(runtime.store.async_list(include_acknowledged=False)):
        live_id = record.get("live_id")
        if live_id and not live_id.startswith(CHAT_ALERT_LIVE_ID_PREFIX):
            await async_acknowledge(hass, runtime, record["id"], "restart")


async def _send_clear_notification(hass: HomeAssistant, notify_service: str, record_id: str) -> None:
    for attempt in (1, 2):
        try:
            await hass.services.async_call(
                "notify",
                notify_service,
                {"message": "clear_notification", "data": {"tag": record_id}},
                blocking=True,
            )
            return
        except Exception:
            if attempt == 1:
                _LOGGER.debug("Sentinel clear_notification to notify.%s failed, retrying", notify_service)
                await asyncio.sleep(2)
            else:
                _LOGGER.exception("Sentinel failed clearing push on notify.%s", notify_service)


async def _clear_mobile_notifications(hass: HomeAssistant, record: NotificationRecord) -> None:
    services = {
        delivery["notify_service"]
        for delivery in (record.get("recipients") or {}).values()
        if delivery.get("dispatched") and _is_mobile_app_target(delivery.get("notify_service") or "")
    }
    await asyncio.gather(*(_send_clear_notification(hass, s, record["id"]) for s in services))


async def async_clear_inactive_live_recipients(hass: HomeAssistant, runtime: SentinelData) -> None:
    timeout = _inactivity_timeout(runtime.options)
    if timeout is None:
        return
    recipients = {r["id"]: r for r in runtime.options.get(CONF_RECIPIENTS, [])}
    for record in runtime.store.async_list(include_acknowledged=False):
        if not record.get("live_id") or not record.get("interactive_only"):
            continue
        changed = False
        for recipient_id, delivery in (record.get("recipients") or {}).items():
            service = delivery.get("notify_service") or ""
            recipient = recipients.get(recipient_id)
            if not delivery.get("dispatched") or not _is_mobile_app_target(service) or recipient is None:
                continue
            if not _is_inactive(hass, recipient, timeout):
                continue
            _LOGGER.debug("Sentinel clearing live %s on notify.%s (inactive)", record["id"], service)
            await _send_clear_notification(hass, service, record["id"])
            delivery["dispatched"] = False
            changed = True
        if changed:
            await runtime.store.async_update_record(record)


async def async_acknowledge(
    hass: HomeAssistant, runtime: SentinelData, notification_id: str, via: str
) -> NotificationRecord | None:
    record = await runtime.store.async_acknowledge(notification_id, via)
    if record is None:
        return None
    _LOGGER.debug("Sentinel acknowledged %s via %s", notification_id, via)
    if record.get("persistent"):
        persistent_notification.async_dismiss(hass, notification_id)
    if record.get("clear_on_acknowledge", True):
        await _clear_mobile_notifications(hass, record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)
    return record


@callback
def async_register_persistent_notification_listener(
    hass: HomeAssistant, runtime: SentinelData
) -> Callable[[], None]:
    """Acknowledge when a Sentinel persistent notification is dismissed in the HA UI."""

    @callback
    def _on_update(update_type: UpdateType, notifications: dict[str, Any]) -> None:
        if update_type != UpdateType.REMOVED:
            return
        for notification_id in notifications:
            record = runtime.store.async_get(notification_id)
            if record and record.get("persistent") and not record.get("acknowledged"):
                hass.async_create_task(async_acknowledge(hass, runtime, notification_id, "ha_ui"))

    return async_dispatcher_connect(hass, SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED, _on_update)


async def async_delete_notifications(
    hass: HomeAssistant, runtime: SentinelData, notification_ids: list[str]
) -> list[str]:
    for notification_id in notification_ids:
        record = runtime.store.async_get(notification_id)
        if record is None or not is_outstanding(record):
            continue
        if record.get("persistent"):
            persistent_notification.async_dismiss(hass, notification_id)
        await _clear_mobile_notifications(hass, record)
    deleted = await runtime.store.async_delete(notification_ids)
    if deleted:
        async_dispatcher_send(hass, SIGNAL_DELETED, deleted)
    return deleted
