from datetime import time

from custom_components.sentinel.routing import (
    in_quiet_hours,
    is_outstanding,
    normalize_keys,
    path_safe,
    recipient_matches,
)

CHANNEL = {"key": "security", "min_importance": "low", "enabled": True}
RECIPIENT = {"id": "r1", "name": "Phone", "min_importance": "low"}


def test_normalize_keys():
    assert normalize_keys(" Front Door ") == ["front_door"]
    assert normalize_keys(["A", "a", "", None, "b c"]) == ["a", "b_c"]
    assert normalize_keys(None) == []


def test_path_safe_blocks_traversal():
    assert path_safe("../../secrets") == "______secrets"
    assert "/" not in path_safe("a/b")
    assert path_safe("") == "_"


def test_quiet_hours_crossing_midnight():
    recipient = {"quiet_start": "22:00:00", "quiet_end": "07:00"}
    assert in_quiet_hours(recipient, time(23, 30))
    assert in_quiet_hours(recipient, time(6, 59))
    assert not in_quiet_hours(recipient, time(12, 0))
    assert not in_quiet_hours({"quiet_start": "bad", "quiet_end": "07:00"}, time(1, 0))


def _matches(record, recipient=RECIPIENT, now=time(12, 0), **kwargs):
    return recipient_matches(
        recipient, record, [CHANNEL], is_home=kwargs.get("home", True), is_inactive=kwargs.get("inactive", False), now=now
    )


def test_recipient_matches_quiet_hours_raise_threshold():
    recipient = {**RECIPIENT, "quiet_start": "22:00", "quiet_end": "07:00", "quiet_min_importance": "high"}
    assert _matches({"importance": "normal"}, recipient, now=time(12, 0))
    assert not _matches({"importance": "normal"}, recipient, now=time(23, 0))
    assert _matches({"importance": "critical"}, recipient, now=time(23, 0))


def test_recipient_matches_presence_and_interactivity():
    assert not _matches({"importance": "normal", "home_only": True}, home=False)
    assert not _matches({"importance": "normal", "interactive_only": True}, inactive=True)
    assert _matches({"importance": "normal", "interactive_only": True}, inactive=False)


def test_allowed_channels_and_per_channel_importance():
    recipient = {**RECIPIENT, "allowed_channels": ["security"], "channel_importance": {"security": "high"}}
    assert not _matches({"importance": "normal"}, recipient)
    assert _matches({"importance": "high"}, recipient)
    assert not _matches({"importance": "critical"}, {**RECIPIENT, "allowed_channels": ["info"]})


def test_is_outstanding():
    assert is_outstanding({"requires_ack": True, "acknowledged": False})
    assert is_outstanding({"live_id": "x", "acknowledged": False})
    assert not is_outstanding({"requires_ack": True, "acknowledged": True})
    assert not is_outstanding({"acknowledged": False})
