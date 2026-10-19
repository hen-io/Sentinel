from __future__ import annotations

from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import chat_websocket_api, legacy, migration, websocket_api  # noqa: F401 (registers ws commands)
from .calendar_scheduler import async_setup_calendar_notifications
from .chat_store import ChatStore
from .config_snapshot import async_write_config_snapshot
from .const import (
    ACK_ACTION_PREFIX,
    CONF_CHANNELS,
    CONF_CHAT_HISTORY_MAX_DAYS,
    CONF_CHAT_HISTORY_MAX_MESSAGES,
    CONF_CHATROOMS,
    CONF_HISTORY_MAX_RECORDS,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_STORAGE_PATH,
    DATABASE_FILENAME,
    DEFAULT_CHAT_HISTORY_MAX_DAYS,
    DEFAULT_CHAT_HISTORY_MAX_MESSAGES,
    DEFAULT_HISTORY_MAX_RECORDS,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DOMAIN,
    SIGNAL_CONFIG_UPDATED,
    SIGNAL_DELETED,
)
from .database import Database
from .dispatch import (
    async_acknowledge,
    async_clear_inactive_live_recipients,
    async_end_live_notifications,
    async_register_persistent_notification_listener,
    async_restore_persistent_notifications,
)
from .images import async_delete_chat_images, async_prune_orphaned_images
from .options import normalize_options
from .runtime import SentinelConfigEntry, SentinelData
from .scheduler import async_setup_scheduled_notifications
from .services import SERVICES, async_register_service, async_register_services
from .store import NotificationStore
from .ws import async_register_commands

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

MOBILE_APP_ACTION_EVENT = "mobile_app_notification_action"
PRUNE_INTERVAL = timedelta(hours=1)
INACTIVITY_CHECK_INTERVAL = timedelta(minutes=1)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_register_commands(hass)
    return True


async def _async_open_storage(hass: HomeAssistant, entry: SentinelConfigEntry) -> SentinelData:
    storage_dir = migration.resolve_storage_dir(hass, entry.options.get(CONF_STORAGE_PATH))
    await hass.async_add_executor_job(migration.migrate_storage_dir, hass, storage_dir)
    await hass.async_add_executor_job(migration.copy_legacy_images, hass)  # legacy

    db = Database(hass, storage_dir / DATABASE_FILENAME)
    await db.async_open()
    runtime = SentinelData(entry=entry, db=db, store=NotificationStore(db), chat_store=ChatStore(db))
    await runtime.store.async_load()
    await runtime.chat_store.async_load()

    if db.created:
        imported = await hass.async_add_executor_job(migration.import_legacy_json, hass, storage_dir)
        if imported:
            await runtime.store.async_add_many(imported)

    records, messages = await hass.async_add_executor_job(  # legacy
        migration.rewrite_image_urls, hass, runtime.store.async_list(), runtime.chat_store.async_all_messages()
    )
    for record in records:
        await runtime.store.async_update_record(record)
    for message in messages:
        await runtime.chat_store.async_update_message(message)
    return runtime


async def async_setup_entry(hass: HomeAssistant, entry: SentinelConfigEntry) -> bool:
    await legacy.async_adopt_legacy_entries(hass, entry)  # legacy

    options = normalize_options(entry.options)
    if options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=options)

    runtime = await _async_open_storage(hass, entry)
    entry.runtime_data = runtime
    entry.async_on_unload(runtime.db.async_close)
    await _async_refresh_user_names(hass, runtime)
    await async_write_config_snapshot(hass, runtime.storage_dir, entry.options)

    async def _on_started(_event: Event | None = None) -> None:
        await async_end_live_notifications(hass, runtime)
        async_restore_persistent_notifications(hass, runtime)
        await _async_prune(hass, runtime)

    if hass.is_running:
        # Added or reloaded: live notifications are still live, keep them.
        async_restore_persistent_notifications(hass, runtime)
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started))

    ack_prefixes = (ACK_ACTION_PREFIX, *legacy.ack_action_prefixes())  # legacy

    async def _on_mobile_action(event: Event) -> None:
        action = event.data.get("action")
        if not isinstance(action, str):
            return
        for prefix in ack_prefixes:
            if action.startswith(prefix):
                await async_acknowledge(hass, runtime, action[len(prefix) :], "mobile_action")
                return

    async def _on_prune(_now) -> None:
        await _async_prune(hass, runtime)

    async def _on_check_inactive(_now) -> None:
        await async_clear_inactive_live_recipients(hass, runtime)

    async def _on_user_changed(_event: Event) -> None:
        await _async_refresh_user_names(hass, runtime)

    for unsub in (
        hass.bus.async_listen(MOBILE_APP_ACTION_EVENT, _on_mobile_action),
        *(hass.bus.async_listen(e, _on_user_changed) for e in ("user_added", "user_updated", "user_removed")),
        async_track_time_interval(hass, _on_prune, PRUNE_INTERVAL),
        async_track_time_interval(hass, _on_check_inactive, INACTIVITY_CHECK_INTERVAL),
        async_register_persistent_notification_listener(hass, runtime),
        async_setup_scheduled_notifications(hass, runtime),
        async_setup_calendar_notifications(hass, runtime),
        entry.add_update_listener(_async_reload_entry),
    ):
        entry.async_on_unload(unsub)

    legacy.async_register_legacy_services(hass, SERVICES, async_register_service)  # legacy
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.async_create_task(legacy.async_check_legacy_folder(hass))  # legacy

    async_dispatcher_send(hass, SIGNAL_CONFIG_UPDATED, websocket_api.config_payload(entry.options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SentinelConfigEntry) -> bool:
    legacy.async_unregister_legacy_services(hass, SERVICES)  # legacy
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: SentinelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_refresh_user_names(hass: HomeAssistant, runtime: SentinelData) -> None:
    runtime.user_names = {user.id: user.name for user in await hass.auth.async_get_users()}


async def _async_prune(hass: HomeAssistant, runtime: SentinelData) -> None:
    options = runtime.options
    pruned = await runtime.store.async_prune(
        options.get(CONF_CHANNELS, []),
        options.get(CONF_HISTORY_MAX_RECORDS, DEFAULT_HISTORY_MAX_RECORDS),
        options.get(CONF_HISTORY_RETENTION_DAYS, DEFAULT_HISTORY_RETENTION_DAYS),
    )
    if pruned:
        async_dispatcher_send(hass, SIGNAL_DELETED, pruned)
    removed = await runtime.chat_store.async_prune(
        options.get(CONF_CHATROOMS, []),
        options.get(CONF_CHAT_HISTORY_MAX_DAYS, DEFAULT_CHAT_HISTORY_MAX_DAYS),
        options.get(CONF_CHAT_HISTORY_MAX_MESSAGES, DEFAULT_CHAT_HISTORY_MAX_MESSAGES),
    )
    for chatroom_id, message_ids in removed.items():
        await async_delete_chat_images(hass, chatroom_id, message_ids)
    await async_prune_orphaned_images(hass, runtime.store.async_list())
