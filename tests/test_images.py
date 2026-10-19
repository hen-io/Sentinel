from __future__ import annotations

import base64

from homeassistant.core import HomeAssistant

from custom_components.sentinel.images import (
    async_prune_orphaned_images,
    async_save_chat_image,
    images_root,
    path_for_url,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def test_path_for_url_rejects_traversal(hass: HomeAssistant):
    assert path_for_url(hass, "/local/sentinel/../secrets.yaml") is None
    assert path_for_url(hass, "/local/other/x.jpg") is None
    assert path_for_url(hass, "/local/sentinel/chat/r/m.png") == images_root(hass) / "chat/r/m.png"


async def test_chat_image_validated_and_survives_prune(hass: HomeAssistant):
    assert await async_save_chat_image(hass, "room", "m0", base64.b64encode(b"<svg>").decode()) is None

    url = await async_save_chat_image(hass, "room", "m1", base64.b64encode(PNG).decode())
    assert url == "/local/sentinel/chat/room/m1.png"
    orphan = images_root(hass) / "notifications" / "info" / "orphan.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")

    await async_prune_orphaned_images(hass, [])
    assert path_for_url(hass, url).is_file()
    assert not orphan.exists()
