"""Pure routing helpers: key normalization, importance and recipient matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import time
from typing import Any

from .const import DEFAULT_QUIET_MIN_IMPORTANCE, IMPORTANCE_LEVELS

_UNSAFE_PATH_CHARS = re.compile(r"[^a-z0-9_-]")


def importance_rank(level: str | None) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


def normalize_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def normalize_keys(values: Any) -> list[str]:
    """Normalize a str or list of str into unique, non-empty keys (order kept)."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    keys: list[str] = []
    for value in values:
        key = normalize_key(value)
        if key and key not in keys:
            keys.append(key)
    return keys


def path_safe(value: str) -> str:
    """Single path component safe to use inside www/ (no traversal)."""
    return _UNSAFE_PATH_CHARS.sub("_", normalize_key(value)) or "_"


def parse_time(value: Any) -> time | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parts = [int(part) for part in value.split(":")]
        return time(*parts[:3])
    except (TypeError, ValueError):
        return None


def in_quiet_hours(recipient: dict[str, Any], now: time) -> bool:
    start = parse_time(recipient.get("quiet_start"))
    end = parse_time(recipient.get("quiet_end"))
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def recipient_targeted(recipient: dict[str, Any], targets: Iterable[str]) -> bool:
    wanted = {t.strip().lower() for t in targets if t and t.strip()}
    return (
        recipient["id"].lower() in wanted
        or recipient.get("name", "").strip().lower() in wanted
    )


def channel_accepts(
    recipient: dict[str, Any], channel: dict[str, Any], rank: int, quiet: bool
) -> bool:
    if not channel.get("enabled", True):
        return False
    allowed = recipient.get("allowed_channels") or []
    if allowed and channel["key"] not in allowed:
        return False
    per_channel = (recipient.get("channel_importance") or {}).get(channel["key"])
    required = max(
        importance_rank(per_channel or recipient.get("min_importance")),
        importance_rank(channel.get("min_importance", "low")),
    )
    if quiet:
        required = max(
            required,
            importance_rank(recipient.get("quiet_min_importance") or DEFAULT_QUIET_MIN_IMPORTANCE),
        )
    return rank >= required


def recipient_matches(
    recipient: dict[str, Any],
    record: dict[str, Any],
    channels: list[dict[str, Any]],
    *,
    is_home: bool,
    is_inactive: bool,
    now: time,
) -> bool:
    """Whether an enabled, targeted recipient should get this record."""
    if record.get("home_only") and not is_home:
        return False
    if record.get("interactive_only") and is_inactive:
        return False
    rank = importance_rank(record.get("importance"))
    quiet = in_quiet_hours(recipient, now)
    return any(channel_accepts(recipient, channel, rank, quiet) for channel in channels)


def is_outstanding(record: dict[str, Any]) -> bool:
    """Still pending: needs acknowledgement, or an active live notification."""
    if record.get("acknowledged"):
        return False
    return bool(record.get("requires_ack") or record.get("live_id"))


def record_channels(record: dict[str, Any]) -> list[str]:
    return list(record.get("channels") or [record.get("channel")])
