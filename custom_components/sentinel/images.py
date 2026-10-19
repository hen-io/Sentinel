"""Image files under www/sentinel: notifications/<channel>/ and chat/<room>/ (served as /local/)."""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Iterable
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CHAT_IMAGES_SUBDIR, IMAGES_DIR_NAME, IMAGES_WEB_PATH, NOTIFICATION_IMAGES_SUBDIR
from .models import NotificationRecord
from .routing import path_safe

_LOGGER = logging.getLogger(__name__)

_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

MAX_CHAT_IMAGE_BYTES = 6 * 1024 * 1024


def images_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("www", IMAGES_DIR_NAME))


def url_for(relative: str) -> str:
    return f"{IMAGES_WEB_PATH}/{relative}"


def path_for_url(hass: HomeAssistant, url: str | None) -> Path | None:
    prefix = f"{IMAGES_WEB_PATH}/"
    if not url or not url.startswith(prefix):
        return None
    relative = url[len(prefix) :].split("?", 1)[0]
    if any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    return images_root(hass) / relative


def sniff_extension(content: bytes) -> str | None:
    """File extension from the image's magic bytes, or None if not a supported image."""
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def _fetch_entity_image(hass: HomeAssistant, entity_id: str) -> tuple[bytes, str] | None:
    domain = entity_id.split(".", 1)[0]
    try:
        if domain == "camera":
            from homeassistant.components import camera

            image = await camera.async_get_image(hass, entity_id)
        elif domain == "image":
            from homeassistant.components import image as image_component

            image = await image_component.async_get_image(hass, entity_id)
        else:
            _LOGGER.warning("Sentinel: image_entity '%s' is not a camera or image entity", entity_id)
            return None
    except Exception:
        _LOGGER.exception("Sentinel failed fetching image from %s", entity_id)
        return None
    return image.content, image.content_type


async def async_capture_entity_image(
    hass: HomeAssistant, record: NotificationRecord, entity_id: str
) -> None:
    fetched = await _fetch_entity_image(hass, entity_id)
    if fetched is None:
        return
    content, content_type = fetched
    extension = _CONTENT_TYPE_EXTENSIONS.get(content_type, "jpg")
    timestamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%S%f")
    relative = (
        f"{NOTIFICATION_IMAGES_SUBDIR}/{path_safe(record['channel'])}/"
        f"{record['id']}_{timestamp}.{extension}"
    )
    await hass.async_add_executor_job(_write, images_root(hass) / relative, content)
    record["image"] = url_for(relative)
    record["image_managed"] = True


async def async_save_chat_image(
    hass: HomeAssistant, chatroom_id: str, message_id: str, content_b64: str
) -> str | None:
    try:
        content = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        _LOGGER.warning("Sentinel: failed decoding chat image for message %s", message_id)
        return None
    if len(content) > MAX_CHAT_IMAGE_BYTES:
        _LOGGER.warning("Sentinel: chat image for message %s is too large", message_id)
        return None
    extension = sniff_extension(content)
    if extension is None:
        _LOGGER.warning("Sentinel: chat image for message %s is not a supported image", message_id)
        return None
    relative = f"{CHAT_IMAGES_SUBDIR}/{path_safe(chatroom_id)}/{message_id}.{extension}"
    await hass.async_add_executor_job(_write, images_root(hass) / relative, content)
    return url_for(relative)


def _delete_chat_image_files(room_dir: Path, message_ids: list[str]) -> None:
    if not room_dir.is_dir():
        return
    for message_id in message_ids:
        for path in room_dir.glob(f"{message_id}.*"):
            try:
                path.unlink()
            except OSError:
                _LOGGER.exception("Sentinel failed deleting chat image %s", path)


async def async_delete_chat_images(
    hass: HomeAssistant, chatroom_id: str, message_ids: list[str]
) -> None:
    if message_ids:
        room_dir = images_root(hass) / CHAT_IMAGES_SUBDIR / path_safe(chatroom_id)
        await hass.async_add_executor_job(_delete_chat_image_files, room_dir, message_ids)


def _delete_unreferenced(root: Path, referenced: set[Path]) -> int:
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and path not in referenced:
            try:
                path.unlink()
                removed += 1
            except OSError:
                _LOGGER.exception("Sentinel failed deleting orphaned image %s", path)
    for directory in root.iterdir():
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


async def async_prune_orphaned_images(
    hass: HomeAssistant, records: Iterable[NotificationRecord]
) -> None:
    """Delete notification images no stored record references (chat images are untouched)."""
    referenced = {
        path
        for record in records
        if record.get("image_managed")
        and (path := path_for_url(hass, record.get("image"))) is not None
    }
    root = images_root(hass) / NOTIFICATION_IMAGES_SUBDIR
    removed = await hass.async_add_executor_job(_delete_unreferenced, root, referenced)
    if removed:
        _LOGGER.info("Sentinel removed %d orphaned notification image(s)", removed)
