"""One-time data migrations: storage folder, K93 ANS images and pre-SQLite JSON history."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CHAT_IMAGES_SUBDIR,
    DATABASE_FILENAME,
    IMAGES_WEB_PATH,
    NOTIFICATION_IMAGES_SUBDIR,
    STORAGE_DIR_NAME,
)
from .images import images_root, path_for_url
from .legacy import LEGACY_IMAGES_DIR_NAME, LEGACY_STORAGE_DIR_NAME  # legacy
from .models import ChatMessage, NotificationRecord

_LOGGER = logging.getLogger(__name__)

_LEGACY_JSON_FILENAMES = ("database.json", "k93_ans_notifications.json")
_LEGACY_STORAGE_KEY = "k93_ans_notifications"
_LEGACY_IMAGES_WEB_PREFIX = f"/local/{LEGACY_IMAGES_DIR_NAME}/"


def default_storage_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_DIR_NAME))


def legacy_storage_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(LEGACY_STORAGE_DIR_NAME))


def resolve_storage_dir(hass: HomeAssistant, storage_path: str | None) -> Path:
    default_dir = default_storage_dir(hass)
    if not storage_path or not storage_path.strip():
        return default_dir
    directory = Path(storage_path.strip())
    if not directory.is_absolute():
        directory = Path(hass.config.path(str(directory)))
    if directory.exists() and not directory.is_dir():
        _LOGGER.error(
            "Sentinel: storage_path %s is a file, not a folder - using %s", directory, default_dir
        )
        return default_dir
    return directory


def _has_data(directory: Path) -> bool:
    return (directory / DATABASE_FILENAME).exists() or any(
        (directory / name).exists() for name in _LEGACY_JSON_FILENAMES
    )


def migrate_storage_dir(hass: HomeAssistant, target: Path) -> None:
    """Fill an empty target folder: move from the default folder (user changed storage_path),
    or copy from the K93 ANS folder (the old folder is kept as a backup)."""
    if _has_data(target):
        return
    default_dir = default_storage_dir(hass)
    try:
        if target != default_dir and _has_data(default_dir):
            target.mkdir(parents=True, exist_ok=True)
            for item in default_dir.iterdir():
                shutil.move(str(item), target / item.name)
            _LOGGER.warning("Sentinel moved its data from %s to %s", default_dir, target)
            return
        legacy_dir = legacy_storage_dir(hass)
        if _has_data(legacy_dir):
            shutil.copytree(legacy_dir, target, dirs_exist_ok=True)
            _LOGGER.warning("Sentinel copied K93 ANS data from %s to %s (old folder kept)", legacy_dir, target)
    except OSError:
        _LOGGER.exception("Sentinel failed migrating its data folder to %s", target)


def _extract_notifications(data: Any) -> list[NotificationRecord]:
    if not isinstance(data, dict):
        return []
    if "notifications" in data:
        return data.get("notifications") or []
    inner = data.get("data")
    return (inner.get("notifications") or []) if isinstance(inner, dict) else []


def import_legacy_json(hass: HomeAssistant, directory: Path) -> list[NotificationRecord]:
    """Records from pre-SQLite JSON history files; each file is renamed *.migrated afterwards."""
    candidates = [directory / name for name in _LEGACY_JSON_FILENAMES]
    candidates.append(Path(hass.config.path(".storage", _LEGACY_STORAGE_KEY)))
    collected: dict[str, NotificationRecord] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            records = _extract_notifications(json.loads(path.read_text(encoding="utf-8")))
            path.rename(path.with_name(path.name + ".migrated"))
        except (OSError, ValueError):
            _LOGGER.exception("Sentinel failed importing legacy history from %s", path)
            continue
        for record in records:
            if isinstance(record, dict) and record.get("id"):
                collected.setdefault(record["id"], record)
        _LOGGER.warning("Sentinel imported %d notification(s) from %s", len(records), path)
    return list(collected.values())


# Images: www/K93-Advanced-Notification-System/{<channel>/<file>, chat/<room>/<file>}
#      -> www/sentinel/{notifications/<channel>/<file>, chat/<room>/<file>}


def _new_relative(legacy_relative: str) -> str:
    parts = legacy_relative.split("/")
    if len(parts) == 3 and parts[0] == CHAT_IMAGES_SUBDIR:
        return legacy_relative
    return f"{NOTIFICATION_IMAGES_SUBDIR}/{legacy_relative}"


def copy_legacy_images(hass: HomeAssistant) -> None:
    legacy_root = Path(hass.config.path("www", LEGACY_IMAGES_DIR_NAME))
    new_root = images_root(hass)
    if not legacy_root.is_dir() or new_root.exists():
        return
    try:
        for path in legacy_root.rglob("*"):
            if path.is_file():
                target = new_root / _new_relative(path.relative_to(legacy_root).as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        _LOGGER.warning("Sentinel copied images from %s to %s (old folder kept)", legacy_root, new_root)
    except OSError:
        _LOGGER.exception("Sentinel failed copying images to %s", new_root)


def rewrite_legacy_image_url(hass: HomeAssistant, url: str | None) -> str | None:
    """New URL for an old K93 ANS image URL whose copied file exists, else None."""
    if not url or not url.startswith(_LEGACY_IMAGES_WEB_PREFIX):
        return None
    new_url = f"{IMAGES_WEB_PATH}/{_new_relative(url[len(_LEGACY_IMAGES_WEB_PREFIX):])}"
    path = path_for_url(hass, new_url)
    return new_url if path is not None and path.is_file() else None


def rewrite_image_urls(
    hass: HomeAssistant, records: list[NotificationRecord], messages: list[ChatMessage]
) -> tuple[list[NotificationRecord], list[ChatMessage]]:
    """Point stored image URLs at the copied images; returns the changed items."""
    changed_records = []
    for record in records:
        if new_url := rewrite_legacy_image_url(hass, record.get("image")):
            record["image"] = new_url
            changed_records.append(record)
    changed_messages = []
    for message in messages:
        if new_url := rewrite_legacy_image_url(hass, message.get("image")):
            message["image"] = new_url
            changed_messages.append(message)
    return changed_records, changed_messages
