"""Websocket command registry: declare once, register under every prefix."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import legacy
from .const import DOMAIN
from .runtime import SentinelData, get_runtime

Handler = Callable[[HomeAssistant, websocket_api.ActiveConnection, dict[str, Any], SentinelData], Any]


@dataclass(frozen=True, slots=True)
class WsCommand:

    name: str
    schema: dict[Any, Any]
    handler: Handler


_COMMANDS: list[WsCommand] = []


def ws_command(name: str, schema: dict[Any, Any] | None = None) -> Callable[[Handler], Handler]:
    """Declare a command; the handler also receives the loaded runtime."""

    def decorate(func: Handler) -> Handler:
        _COMMANDS.append(WsCommand(name, schema or {}, func))
        return func

    return decorate


def _not_loaded(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_error(msg["id"], "not_loaded", "Sentinel is not loaded")


def _wrap(command: WsCommand) -> websocket_api.WebSocketCommandHandler:
    if inspect.iscoroutinefunction(command.handler):

        @websocket_api.async_response
        async def _async_handler(hass: HomeAssistant, connection, msg) -> None:
            if (runtime := get_runtime(hass)) is None:
                _not_loaded(connection, msg)
                return
            await command.handler(hass, connection, msg, runtime)

        return _async_handler

    @callback
    def _handler(hass: HomeAssistant, connection, msg) -> None:
        if (runtime := get_runtime(hass)) is None:
            _not_loaded(connection, msg)
            return
        command.handler(hass, connection, msg, runtime)

    return _handler


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in _COMMANDS:
        handler = _wrap(command)
        for prefix in (DOMAIN, *legacy.ws_prefixes()):  # legacy
            command_type = f"{prefix}/{command.name}"
            schema = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                {vol.Required("type"): command_type, **command.schema}
            )
            websocket_api.async_register_command(hass, command_type, handler, schema)
