from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import CONFIG_SNAPSHOT_FILENAME
from .options import normalize_options
from .runtime import SentinelData

_LOGGER = logging.getLogger(__name__)


def _write_snapshot(path: Path, options: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(options, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def async_write_config_snapshot(
    hass: HomeAssistant, storage_dir: Path, options: Mapping[str, Any]
) -> None:
    path = storage_dir / CONFIG_SNAPSHOT_FILENAME
    try:
        await hass.async_add_executor_job(_write_snapshot, path, dict(options))
    except OSError:
        _LOGGER.exception("Sentinel failed writing config snapshot to %s", path)


async def async_restore_config_from_snapshot(hass: HomeAssistant, runtime: SentinelData) -> None:
    path = runtime.storage_dir / CONFIG_SNAPSHOT_FILENAME
    try:
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
        restored = json.loads(raw)
    except OSError as err:
        raise ServiceValidationError(f"No config snapshot found at {path}") from err
    except ValueError as err:
        raise ServiceValidationError(f"Config snapshot at {path} is not valid JSON: {err}") from err
    if not isinstance(restored, dict):
        raise ServiceValidationError(f"Config snapshot at {path} is not a JSON object")
    hass.config_entries.async_update_entry(runtime.entry, options=normalize_options(restored))
