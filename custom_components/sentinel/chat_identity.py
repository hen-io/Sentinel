from __future__ import annotations

from homeassistant.core import HomeAssistant, State

from .models import ChatMessage


def persons_by_user(hass: HomeAssistant) -> dict[str, State]:
    return {
        user_id: state
        for state in hass.states.async_all("person")
        if (user_id := state.attributes.get("user_id"))
    }


def resolve_sender(
    message: ChatMessage, user_names: dict[str, str], persons: dict[str, State]
) -> dict:
    user_id = message.get("sender_user_id")
    if user_id is None:
        return {
            "name": message.get("sender_name") or "System",
            "icon": message.get("sender_icon") or "mdi:robot",
            "picture": None,
        }
    person = persons.get(user_id)
    return {
        "name": (person.name if person else None) or user_names.get(user_id) or "Unknown user",
        "icon": None,
        "picture": person.attributes.get("entity_picture") if person else None,
    }
