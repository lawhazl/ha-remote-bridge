"""Connect two Home Assistant instances via the Websocket API."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Optional
import copy
import fnmatch
import inspect
import logging
import re
from contextlib import suppress

import aiohttp
from aiohttp import ClientWebSocketResponse
import homeassistant.components.websocket_api as websocket_api
import homeassistant.components.websocket_api.auth as api
import voluptuous as vol

try:
    from homeassistant.core_config import DATA_CUSTOMIZE
except (ModuleNotFoundError, ImportError):
    from homeassistant.config import DATA_CUSTOMIZE

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_ABOVE,
    CONF_BELOW,
    CONF_ENTITY_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_VERIFY_SSL,
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
)
from homeassistant.components import persistent_notification
from homeassistant.core import Context, EventOrigin, HomeAssistant, callback, split_entity_id
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPROVED_REMOTES,
    CONF_ENTITY_PREFIX,
    CONF_ENTITY_FRIENDLY_NAME_PREFIX,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_FILTER,
    CONF_HOST_ENTRY_ID,
    CONF_INCLUDE_DEVICES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_LOAD_COMPONENTS,
    CONF_MAX_MSG_SIZE,
    CONF_ROLE,
    CONF_SECURE,
    CONF_SERVICE_PREFIX,
    CONF_SERVICES,
    CONF_SUBSCRIBE_EVENTS,
    DEFAULT_MAX_MSG_SIZE,
    DOMAIN,
    ROLE_HOST,
    ROLE_REMOTE,
    WS_CMD_GET_EXPOSED_ENTITIES,
)
from .logger import log, async_setup_log_rotation
from .proxy_services import ProxyServices
from .rest_api import UnsupportedVersion, async_get_discovery_info
from .views import DiscoveryInfoView, HostConfigsView, get_integration_version

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

STATE_INIT = "initializing"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_AUTH_INVALID = "auth_invalid"
STATE_AUTH_REQUIRED = "auth_required"
STATE_RECONNECTING = "reconnecting"
STATE_DISCONNECTED = "disconnected"

HEARTBEAT_INTERVAL = 20
HEARTBEAT_TIMEOUT = 5

INTERNALLY_USED_EVENTS = [EVENT_STATE_CHANGED]


@dataclass
class RemoteHomeAssistantData:
    """Runtime data stored in entry.runtime_data."""
    connection: "RemoteConnection | None"


type RemoteHomeAssistantConfigEntry = ConfigEntry[RemoteHomeAssistantData]


@websocket_api.websocket_command({
    vol.Required("type"): WS_CMD_GET_EXPOSED_ENTITIES,
    vol.Required("remote_uuid"): str,
    vol.Optional("remote_name", default="Unknown"): str,
})
@websocket_api.async_response
async def ws_get_exposed_entities(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return filtered entity snapshot for an approved remote, or signal pending approval."""
    remote_uuid = msg["remote_uuid"]
    remote_name = msg.get("remote_name", "Unknown")

    # Check whether any host entry has approved this remote
    approved_entry = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ROLE) == ROLE_HOST:
            if remote_uuid in entry.options.get(CONF_APPROVED_REMOTES, {}):
                approved_entry = entry
                break

    if approved_entry:
        # Dismiss any lingering pending notification
        persistent_notification.async_dismiss(
            hass, f"ha_bridge_pending_{remote_uuid}"
        )
        # Full entity snapshot returned in Stage 3 — stub for now
        connection.send_result(msg["id"], {"status": "ok", "entities": [], "devices": []})
        return

    # Not approved — record as pending and notify the host user
    pending = hass.data[DOMAIN].setdefault("pending_remotes", {})
    is_new = remote_uuid not in pending
    pending[remote_uuid] = {
        "uuid": remote_uuid,
        "name": remote_name,
        "first_seen": pending.get(remote_uuid, {}).get(
            "first_seen", dt_util.utcnow().isoformat()
        ),
    }

    if is_new:
        persistent_notification.async_create(
            hass,
            f"**{remote_name}** is requesting access to Remote Bridge.\n\n"
            "Go to **Settings → Integrations → Remote Bridge** and open "
            "the host configuration to approve it.",
            title="Remote Bridge: Access Request",
            notification_id=f"ha_bridge_pending_{remote_uuid}",
        )

    connection.send_result(msg["id"], {"status": "pending_approval"})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Remote Bridge component."""
    hass.data.setdefault(DOMAIN, {
        "_view_registered": False,
        "_host_configs_view_registered": False,
        "_ws_registered": False,
        "pending_remotes": {},  # {uuid: {uuid, name, first_seen}} — cleared on HA restart
    })

    async def _handle_dump_diagnostics(service) -> None:
        _LOGGER.info("[%s] dump_diagnostics called — full implementation in Stage 8", DOMAIN)

    async_register_admin_service(hass, DOMAIN, "dump_diagnostics", _handle_dump_diagnostics)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Remote Home-Assistant from a config entry."""
    role = entry.data.get(CONF_ROLE)
    version = get_integration_version()

    if role == ROLE_HOST:
        if not hass.data[DOMAIN].get("_view_registered"):
            hass.http.register_view(DiscoveryInfoView())
            hass.data[DOMAIN]["_view_registered"] = True
            log(hass, "HOST", "STARTUP", f"Discovery endpoint registered: /api/ha_bridge/discovery")

        if not hass.data[DOMAIN].get("_host_configs_view_registered"):
            hass.http.register_view(HostConfigsView())
            hass.data[DOMAIN]["_host_configs_view_registered"] = True
            log(hass, "HOST", "STARTUP", "Host configs endpoint registered: /api/ha_bridge/host_configs")

        if not hass.data[DOMAIN].get("_ws_registered"):
            websocket_api.async_register_command(hass, ws_get_exposed_entities)
            hass.data[DOMAIN]["_ws_registered"] = True
            log(hass, "HOST", "STARTUP", f"WebSocket command registered: {WS_CMD_GET_EXPOSED_ENTITIES}")

        entry.runtime_data = RemoteHomeAssistantData(connection=None)

        include_domains = entry.options.get(CONF_INCLUDE_DOMAINS, [])
        include_devices = entry.options.get(CONF_INCLUDE_DEVICES, [])
        include_entities = entry.options.get(CONF_INCLUDE_ENTITIES, [])
        log(
            hass, "HOST", "STARTUP",
            f"v{version} starting in HOST mode — entry: {entry.title!r} | "
            f"filter: {len(include_domains)} domains, {len(include_devices)} devices, "
            f"{len(include_entities)} entities",
        )

        if not hass.data[DOMAIN].get("_log_rotation_started"):
            hass.data[DOMAIN]["_log_rotation_started"] = True
            await async_setup_log_rotation(hass)
        return True

    if role == ROLE_REMOTE:
        remote = RemoteConnection(hass, entry)
        entry.runtime_data = RemoteHomeAssistantData(connection=remote)

        log(
            hass, "REMOTE", "STARTUP",
            f"v{version} starting in REMOTE mode, target: "
            f"{entry.data.get(CONF_HOST)}:{entry.data.get(CONF_PORT)}",
        )

        async def _setup() -> None:
            for domain in entry.options.get(CONF_LOAD_COMPONENTS, []):
                hass.async_create_task(async_setup_component(hass, domain, {}))
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
            await remote.async_connect()

        hass.async_create_task(_setup())
        return True

    _LOGGER.error("Unknown role %r for entry %s — cannot set up", role, entry.entry_id)
    return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    role = entry.data.get(CONF_ROLE)

    if role == ROLE_HOST:
        return True

    if role == ROLE_REMOTE:
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            return False
        connection = entry.runtime_data.connection
        if connection is not None:
            await connection.async_stop()
        return True

    return False


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow user to remove a mirrored device from the UI."""
    _LOGGER.info(
        "[REMOTE] Device %r manually removed by user via UI", device_entry.name
    )
    return True


class RemoteConnection:
    """A WebSocket connection to a remote Home Assistant instance."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the connection."""
        self._hass = hass
        self._entry = config_entry
        self._secure = config_entry.data.get(CONF_SECURE, False)
        self._verify_ssl = config_entry.data.get(CONF_VERIFY_SSL, False)
        self._access_token = config_entry.data.get(CONF_ACCESS_TOKEN)
        self._max_msg_size = config_entry.data.get(CONF_MAX_MSG_SIZE, DEFAULT_MAX_MSG_SIZE)

        self._whitelist_e = set(config_entry.options.get(CONF_INCLUDE_ENTITIES, []))
        self._whitelist_d = set(config_entry.options.get(CONF_INCLUDE_DOMAINS, []))
        self._blacklist_e = set(config_entry.options.get(CONF_EXCLUDE_ENTITIES, []))
        self._blacklist_d = set(config_entry.options.get(CONF_EXCLUDE_DOMAINS, []))

        self._filter = [
            {
                CONF_ENTITY_ID: re.compile(fnmatch.translate(f.get(CONF_ENTITY_ID)))
                if f.get(CONF_ENTITY_ID)
                else None,
                CONF_UNIT_OF_MEASUREMENT: f.get(CONF_UNIT_OF_MEASUREMENT),
                CONF_ABOVE: f.get(CONF_ABOVE),
                CONF_BELOW: f.get(CONF_BELOW),
            }
            for f in config_entry.options.get(CONF_FILTER, [])
        ]

        self._subscribe_events = set(
            config_entry.options.get(CONF_SUBSCRIBE_EVENTS, []) + INTERNALLY_USED_EVENTS
        )
        self._entity_prefix = config_entry.options.get(CONF_ENTITY_PREFIX, "")
        self._entity_friendly_name_prefix = config_entry.options.get(
            CONF_ENTITY_FRIENDLY_NAME_PREFIX, ""
        )

        self._connection: Optional[ClientWebSocketResponse] = None
        self._heartbeat_task = None
        self._is_stopping = False
        self._entities: set[str] = set()
        self._all_entity_names: set[str] = set()
        self._handlers: dict = {}
        self._remove_listener = None
        self.proxy_services = ProxyServices(hass, config_entry, self)

        self.set_connection_state(STATE_CONNECTING)

        self.__id = 1

    def _prefixed_entity_id(self, entity_id: str) -> str:
        if self._entity_prefix:
            domain, object_id = split_entity_id(entity_id)
            object_id = self._entity_prefix + object_id
            return domain + "." + object_id
        return entity_id

    def _prefixed_entity_friendly_name(self, entity_friendly_name: str) -> str:
        if (
            self._entity_friendly_name_prefix
            and not entity_friendly_name.startswith(self._entity_friendly_name_prefix)
        ):
            return self._entity_friendly_name_prefix + entity_friendly_name
        return entity_friendly_name

    def _full_picture_url(self, url: str) -> str:
        if re.match(r"^https?://", url):
            return url
        base = "%s://%s:%s" % (
            "https" if self._secure else "http",
            self._entry.data[CONF_HOST],
            self._entry.data[CONF_PORT],
        )
        if url.startswith(base):
            return url
        return base + url

    def set_connection_state(self, state: str) -> None:
        """Broadcast current connection state via dispatcher.

        Thread-safe: uses call_soon_threadsafe so this method can be called
        from any context (event loop or executor thread) without triggering
        the HA async_write_ha_state thread-safety check.
        """
        signal = f"ha_bridge_{self._entry.unique_id}"
        self._hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self._hass, signal, state
        )

    @callback
    def _get_url(self) -> str:
        return "%s://%s:%s/api/websocket" % (
            "wss" if self._secure else "ws",
            self._entry.data[CONF_HOST],
            self._entry.data[CONF_PORT],
        )

    async def async_connect(self) -> None:
        """Connect to remote Home Assistant WebSocket."""

        async def _async_stop_handler(event):
            await self.async_stop()

        host = self._entry.data[CONF_HOST]
        port = self._entry.data[CONF_PORT]

        async def _async_instance_get_info():
            try:
                return await async_get_discovery_info(
                    self._hass, host, port, self._secure,
                    self._access_token, self._verify_ssl,
                )
            except OSError as err:
                # Connection refused / network unreachable — expected during
                # reconnect loops. Log as WARNING (no traceback) to avoid spam.
                _LOGGER.warning(
                    "Cannot reach host at %s:%s — %s. Will retry in 10 s.",
                    host, port, err,
                )
            except UnsupportedVersion:
                _LOGGER.error(
                    "Host at %s:%s requires Home Assistant 0.111 or newer.",
                    host, port,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Unexpected error fetching info from %s:%s — %s. Will retry in 10 s.",
                    host, port, err,
                )
            return None

        @callback
        def _async_instance_id_match(info) -> bool:
            if not info:
                return False
            if info["uuid"] != self._entry.unique_id:
                _LOGGER.error(
                    "instance id not matching: %s != %s",
                    info["uuid"],
                    self._entry.unique_id,
                )
                return False
            return True

        url = self._get_url()
        session = async_get_clientsession(self._hass, self._verify_ssl)
        self.set_connection_state(STATE_CONNECTING)

        while True:
            info = await _async_instance_get_info()

            if not _async_instance_id_match(info):
                self.set_connection_state(STATE_RECONNECTING)
                await asyncio.sleep(10)
                continue

            try:
                _LOGGER.info("Connecting to %s", url)
                self._connection = await session.ws_connect(
                    url, max_msg_size=self._max_msg_size
                )
            except aiohttp.client_exceptions.ClientError:
                _LOGGER.error("Could not connect to %s, retry in 10 seconds...", url)
                self.set_connection_state(STATE_RECONNECTING)
                await asyncio.sleep(10)
            else:
                _LOGGER.info("Connected to home-assistant websocket at %s", url)
                break

        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_handler)

        device_registry = dr.async_get(self._hass)
        device_registry.async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={(DOMAIN, f"remote_{self._entry.unique_id}")},
            name=info.get("location_name"),
            manufacturer="Home Assistant",
            model=info.get("installation_type"),
            sw_version=info.get("ha_version"),
        )

        asyncio.ensure_future(self._recv())
        self._heartbeat_task = self._hass.loop.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to keep connection alive."""
        while self._connection is not None and not self._connection.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            _LOGGER.debug("Sending ping")
            event = asyncio.Event()

            def resp(message):
                _LOGGER.debug("Got pong: %s", message)
                event.set()

            await self.call(resp, "ping")

            try:
                await asyncio.wait_for(event.wait(), HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.warning("heartbeat failed")
                asyncio.ensure_future(self._connection.close())
                break

    async def async_stop(self) -> None:
        """Close connection."""
        self._is_stopping = True
        if self._connection is not None:
            await self._connection.close()
        await self.proxy_services.unload()

    def _next_id(self) -> int:
        _id = self.__id
        self.__id += 1
        return _id

    async def call(self, handler, message_type: str, **extra_args) -> None:
        if self._connection is None or self._connection.closed:
            _LOGGER.debug("Dropping call %r — connection not open", message_type)
            return

        _id = self._next_id()
        self._handlers[_id] = handler
        try:
            await self._connection.send_json(
                {"id": _id, "type": message_type, **extra_args}
            )
        except aiohttp.client_exceptions.ClientError as err:
            _LOGGER.debug("remote websocket connection closed: %s", err)
            await self._disconnected()

    async def _disconnected(self) -> None:
        for entity in self._entities:
            self._hass.states.async_remove(entity)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._remove_listener is not None:
            self._remove_listener()

        self.set_connection_state(STATE_DISCONNECTED)
        self._heartbeat_task = None
        self._remove_listener = None
        self._entities = set()
        self._all_entity_names = set()
        if not self._is_stopping:
            asyncio.ensure_future(self.async_connect())

    async def _recv(self) -> None:
        while self._connection is not None and not self._connection.closed:
            try:
                data = await self._connection.receive()
            except aiohttp.client_exceptions.ClientError as err:
                _LOGGER.error("remote websocket connection closed: %s", err)
                break

            if not data:
                break

            if data.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                _LOGGER.debug("websocket connection is closing")
                break

            if data.type == aiohttp.WSMsgType.ERROR:
                _LOGGER.error("websocket connection had an error")
                if data.data.code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG:
                    _LOGGER.error(
                        "please consider increasing message size with `%s`", CONF_MAX_MSG_SIZE
                    )
                break

            try:
                message = data.json()
            except TypeError as err:
                _LOGGER.error("could not decode data (%s) as json: %s", data, err)
                break

            if message is None:
                break

            _LOGGER.debug("received: %s", message)

            if message["type"] == api.TYPE_AUTH_OK:
                self.set_connection_state(STATE_CONNECTED)
                await self._init()

            elif message["type"] == api.TYPE_AUTH_REQUIRED:
                if self._access_token:
                    json_data = {
                        "type": api.TYPE_AUTH,
                        "access_token": self._access_token,
                    }
                else:
                    _LOGGER.error("Access token required, but not provided")
                    self.set_connection_state(STATE_AUTH_REQUIRED)
                    return
                try:
                    await self._connection.send_json(json_data)
                except Exception as err:
                    _LOGGER.error("could not send data to remote connection: %s", err)
                    break

            elif message["type"] == api.TYPE_AUTH_INVALID:
                _LOGGER.error("Auth invalid, check your access token")
                self.set_connection_state(STATE_AUTH_INVALID)
                await self._connection.close()
                return

            else:
                handler = self._handlers.get(message["id"])
                if handler is not None:
                    if inspect.iscoroutinefunction(handler):
                        await handler(message)
                    else:
                        handler(message)

        await self._disconnected()

    async def _init(self) -> None:
        async def forward_event(event):
            """Forward local service calls to remote for mirrored entities."""
            event_data = event.data
            service_data = event_data["service_data"]

            if not service_data:
                return

            entity_ids = service_data.get("entity_id", None)
            if not entity_ids:
                return

            if isinstance(entity_ids, str):
                entity_ids = (entity_ids.lower(),)

            entities = {entity_id.lower() for entity_id in self._entities}
            entity_ids = entities.intersection(entity_ids)

            if not entity_ids:
                return

            if self._entity_prefix:
                def _remove_prefix(entity_id):
                    domain, object_id = split_entity_id(entity_id)
                    object_id = object_id.replace(self._entity_prefix.lower(), "", 1)
                    return domain + "." + object_id
                entity_ids = {_remove_prefix(eid) for eid in entity_ids}

            event_data = copy.deepcopy(event_data)
            event_data["service_data"]["entity_id"] = list(entity_ids)
            event_data.pop("service_call_id", None)

            _id = self._next_id()
            data = {"id": _id, "type": event.event_type, **event_data}
            _LOGGER.debug("forward event: %s", data)

            if self._connection is None or self._connection.closed:
                _LOGGER.debug("Dropping forwarded event — connection not open")
                return
            try:
                await self._connection.send_json(data)
            except Exception as err:
                _LOGGER.debug("could not send data to remote connection: %s", err)
                await self._disconnected()

        def state_changed(entity_id: str, state: str, attr: dict) -> None:
            """Publish remote state change on local instance."""
            domain, _object_id = split_entity_id(entity_id)

            self._all_entity_names.add(entity_id)

            if entity_id in self._blacklist_e or domain in self._blacklist_d:
                return

            if (
                (self._whitelist_e or self._whitelist_d)
                and entity_id not in self._whitelist_e
                and domain not in self._whitelist_d
            ):
                return

            for f in self._filter:
                if f[CONF_ENTITY_ID] and not f[CONF_ENTITY_ID].match(entity_id):
                    continue
                if f[CONF_UNIT_OF_MEASUREMENT]:
                    if CONF_UNIT_OF_MEASUREMENT not in attr:
                        continue
                    if f[CONF_UNIT_OF_MEASUREMENT] != attr[CONF_UNIT_OF_MEASUREMENT]:
                        continue
                try:
                    if f[CONF_BELOW] and float(state) < f[CONF_BELOW]:
                        _LOGGER.info(
                            "%s: ignoring state '%s', because below '%s'",
                            entity_id, state, f[CONF_BELOW],
                        )
                        return
                    if f[CONF_ABOVE] and float(state) > f[CONF_ABOVE]:
                        _LOGGER.info(
                            "%s: ignoring state '%s', because above '%s'",
                            entity_id, state, f[CONF_ABOVE],
                        )
                        return
                except ValueError:
                    pass

            entity_id = self._prefixed_entity_id(entity_id)

            domain, object_id = split_entity_id(entity_id)
            attr["unique_id"] = f"{self._entry.unique_id[:16]}_{entity_id}"
            entity_registry = er.async_get(self._hass)
            entity_registry.async_get_or_create(
                domain=domain,
                platform="ha_bridge",
                unique_id=attr["unique_id"],
                suggested_object_id=object_id,
            )

            if DATA_CUSTOMIZE in self._hass.data:
                attr.update(self._hass.data[DATA_CUSTOMIZE].get(entity_id))

            for attrId, value in attr.items():
                if attrId == "friendly_name":
                    attr[attrId] = self._prefixed_entity_friendly_name(value)
                if attrId == "entity_picture":
                    attr[attrId] = self._full_picture_url(value)

            self._entities.add(entity_id)
            self._hass.states.async_set(entity_id, state, attr)

        def fire_event(message: dict) -> None:
            """Publish remote event on local instance."""
            if message["type"] == "result":
                return
            if message["type"] != "event":
                return

            if message["event"]["event_type"] == "state_changed":
                data = message["event"]["data"]
                entity_id = data["entity_id"]
                if not data["new_state"]:
                    entity_id = self._prefixed_entity_id(entity_id)
                    with suppress(ValueError, AttributeError, KeyError):
                        self._entities.remove(entity_id)
                    with suppress(ValueError, AttributeError, KeyError):
                        self._all_entity_names.remove(entity_id)
                    self._hass.states.async_remove(entity_id)
                    return

                state = data["new_state"]["state"]
                attr = data["new_state"]["attributes"]
                state_changed(entity_id, state, attr)
            else:
                event = message["event"]
                self._hass.bus.async_fire(
                    event_type=event["event_type"],
                    event_data=event["data"],
                    context=Context(
                        id=event["context"].get("id"),
                        user_id=event["context"].get("user_id"),
                        parent_id=event["context"].get("parent_id"),
                    ),
                    origin=EventOrigin.remote,
                )

        def got_states(message: dict) -> None:
            """Process initial list of remote states."""
            for entity in message["result"]:
                entity_id = entity["entity_id"]
                state = entity["state"]
                attributes = entity["attributes"]
                for attr, value in attributes.items():
                    if attr == "friendly_name":
                        attributes[attr] = self._prefixed_entity_friendly_name(value)
                    if attr == "entity_picture":
                        attributes[attr] = self._full_picture_url(value)
                state_changed(entity_id, state, attributes)

        self._remove_listener = self._hass.bus.async_listen(
            EVENT_CALL_SERVICE, forward_event
        )

        for event in self._subscribe_events:
            await self.call(fire_event, "subscribe_events", event_type=event)

        await self.call(got_states, "get_states")
        await self.proxy_services.load()
