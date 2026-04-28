"""Config flow for Remote Home-Assistant integration."""
from __future__ import annotations
import logging
from typing import Any, Mapping
from urllib.parse import urlparse

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, core
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.const import (
    CONF_ABOVE,
    CONF_ACCESS_TOKEN,
    CONF_BELOW,
    CONF_ENTITY_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.instance_id import async_get
from homeassistant.util import slugify

from .const import (
    CONF_ENTITY_PREFIX,
    CONF_ENTITY_FRIENDLY_NAME_PREFIX,
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_FILTER,
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
)
from .rest_api import (
    ApiProblem,
    CannotConnect,
    EndpointMissing,
    InvalidAuth,
    UnsupportedVersion,
    async_get_discovery_info,
    async_probe_host,
)

_LOGGER = logging.getLogger(__name__)

ADD_NEW_EVENT = "add_new_event"
FILTER_OPTIONS = [CONF_ENTITY_ID, CONF_UNIT_OF_MEASUREMENT, CONF_ABOVE, CONF_BELOW]


def _filter_str(index: int, filter_conf: Mapping[str, str | float]) -> str:
    entity_id = filter_conf.get(CONF_ENTITY_ID)
    unit = filter_conf.get(CONF_UNIT_OF_MEASUREMENT)
    above = filter_conf.get(CONF_ABOVE)
    below = filter_conf.get(CONF_BELOW)
    return f"{index + 1}. {entity_id}, unit: {unit}, above: {above}, below: {below}"


async def validate_connection(hass: core.HomeAssistant, conf: dict) -> dict:
    """Validate the user input allows us to connect. Returns discovery info."""
    try:
        info = await async_get_discovery_info(
            hass,
            conf[CONF_HOST],
            conf[CONF_PORT],
            conf.get(CONF_SECURE, False),
            conf[CONF_ACCESS_TOKEN],
            conf.get(CONF_VERIFY_SSL, False),
        )
    except OSError as exc:
        raise CannotConnect() from exc
    return {"title": info["location_name"], "uuid": info["uuid"]}


def _area_grouped_selector(options: dict[str, str]) -> SelectSelector:
    """Searchable multi-select dropdown with options ordered Area › Name.

    Typing an area name in the search box filters to that area's items,
    giving the effect of area grouping alongside full text search.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[{"value": k, "label": v} for k, v in options.items()],
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _local_domains(hass: core.HomeAssistant) -> list[str]:
    return sorted({s.domain for s in hass.states.async_all()})


def _area_name(area_reg: ar.AreaRegistry, area_id: str | None) -> str:
    if area_id:
        area = area_reg.async_get_area(area_id)
        if area:
            return area.name
    return ""


def _local_devices(hass: core.HomeAssistant) -> dict[str, str]:
    area_reg = ar.async_get(hass)
    device_reg = dr.async_get(hass)

    rows: list[tuple[str, str, str]] = []
    for device in device_reg.devices.values():
        name = device.name_by_user or device.name or device.id
        area = _area_name(area_reg, device.area_id)
        rows.append((area, name, device.id))

    rows.sort(key=lambda x: (x[0] == "", x[0].casefold(), x[1].casefold()))

    result: dict[str, str] = {}
    for area, name, device_id in rows:
        label = f"{area} › {name}" if area else name
        result[device_id] = label
    return result


def _local_entities(hass: core.HomeAssistant) -> dict[str, str]:
    area_reg = ar.async_get(hass)
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    rows: list[tuple[str, str, str]] = []
    for entry in entity_reg.entities.values():
        if entry.area_id:
            area = _area_name(area_reg, entry.area_id)
        elif entry.device_id:
            device = device_reg.async_get(entry.device_id)
            area = _area_name(area_reg, device.area_id if device else None)
        else:
            area = ""
        name = entry.name or entry.original_name or entry.entity_id
        rows.append((area, name, entry.entity_id))

    rows.sort(key=lambda x: (x[0] == "", x[0].casefold(), x[1].casefold()))

    result: dict[str, str] = {}
    for area, name, entity_id in rows:
        label = f"{area} › {name}" if area else name
        result[entity_id] = label
    return result


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Remote Home-Assistant."""

    VERSION = 1

    def __init__(self):
        """Initialize a new ConfigFlow."""
        self.prefill: dict[str, Any] = {
            CONF_PORT: 8123,
            CONF_SECURE: True,
            CONF_MAX_MSG_SIZE: DEFAULT_MAX_MSG_SIZE,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow for this handler."""
        if config_entry.data.get(CONF_ROLE) == ROLE_HOST:
            return HostOptionsFlowHandler()
        return RemoteOptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle the initial step — select host or remote mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            role = user_input.get(CONF_ROLE)
            if role == ROLE_HOST:
                return await self.async_step_host_filters()
            elif role == ROLE_REMOTE:
                return await self.async_step_connection_details()
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROLE): vol.In(
                        {ROLE_HOST: "Host mode (exposes entities)", ROLE_REMOTE: "Remote mode (mirrors entities)"}
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_host_filters(self, user_input=None):
        """Configure name and initial include/exclude filters for a host-mode entry."""
        if user_input is not None:
            title = user_input.pop(CONF_NAME)
            return self.async_create_entry(
                title=title,
                data={CONF_ROLE: ROLE_HOST},
                options=user_input,
            )

        domains = _local_domains(self.hass)
        devices = _local_devices(self.hass)
        entities = _local_entities(self.hass)
        default_name = f"{self.hass.config.location_name} (Host)"

        return self.async_show_form(
            step_id="host_filters",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=default_name): str,
                    vol.Optional(CONF_INCLUDE_DOMAINS, default=[]): cv.multi_select(domains),
                    vol.Optional(CONF_INCLUDE_DEVICES, default=[]): _area_grouped_selector(devices),
                    vol.Optional(CONF_INCLUDE_ENTITIES, default=[]): _area_grouped_selector(entities),
                    vol.Optional(CONF_EXCLUDE_DOMAINS, default=[]): cv.multi_select(domains),
                    vol.Optional(CONF_EXCLUDE_DEVICES, default=[]): _area_grouped_selector(devices),
                    vol.Optional(CONF_EXCLUDE_ENTITIES, default=[]): _area_grouped_selector(entities),
                }
            ),
        )

    async def async_step_connection_details(self, user_input=None):
        """Handle connection details for remote mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_connection(self.hass, user_input)
            except ApiProblem:
                errors["base"] = "api_problem"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except UnsupportedVersion:
                errors["base"] = "unsupported_version"
            except EndpointMissing:
                errors["base"] = "missing_endpoint"
            except Exception:
                _LOGGER.exception("Unexpected exception during connection validation")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["uuid"])
                self._abort_if_unique_id_configured()
                title = user_input.pop(CONF_NAME)
                data = {CONF_ROLE: ROLE_REMOTE, **user_input}
                return self.async_create_entry(title=title, data=data)

        host = self.prefill.get(CONF_HOST, vol.UNDEFINED)
        port = self.prefill.get(CONF_PORT, vol.UNDEFINED)
        secure = self.prefill.get(CONF_SECURE, vol.UNDEFINED)
        max_msg_size = self.prefill.get(CONF_MAX_MSG_SIZE, vol.UNDEFINED)
        default_name = f"{self.hass.config.location_name} (Remote)"

        user_input = user_input or {}
        return self.async_show_form(
            step_id="connection_details",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=user_input.get(CONF_NAME, default_name)): str,
                    vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, host)): str,
                    vol.Required(CONF_PORT, default=user_input.get(CONF_PORT, port)): int,
                    vol.Required(
                        CONF_ACCESS_TOKEN,
                        default=user_input.get(CONF_ACCESS_TOKEN, vol.UNDEFINED),
                    ): str,
                    vol.Required(
                        CONF_MAX_MSG_SIZE, default=user_input.get(CONF_MAX_MSG_SIZE, max_msg_size)
                    ): int,
                    vol.Optional(CONF_SECURE, default=user_input.get(CONF_SECURE, secure)): bool,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=user_input.get(CONF_VERIFY_SSL, True),
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info):
        """Handle instance discovered via zeroconf — leads into remote mode flow."""
        properties = discovery_info.properties
        port = discovery_info.port
        uuid = properties["uuid"]

        if await async_get(self.hass) == uuid:
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(uuid)
        self._abort_if_unique_id_configured()

        url = properties.get("internal_url") or properties.get("base_url")
        parsed = urlparse(url)
        host = parsed.hostname
        secure = parsed.scheme == "https"

        try:
            await async_probe_host(self.hass, host, port, secure, False)
        except (EndpointMissing, CannotConnect, ApiProblem):
            return self.async_abort(reason="not_ha_bridge_host")

        self.prefill = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_SECURE: secure,
            CONF_MAX_MSG_SIZE: DEFAULT_MAX_MSG_SIZE,
        }

        self.context["identifier"] = self.unique_id
        self.context["title_placeholders"] = {"name": properties["location_name"]}
        return await self.async_step_connection_details()


class HostOptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Options flow for host-mode entries — 4 steps. Reloads entry on save."""

    def __init__(self):
        self.options: dict[str, Any] | None = None
        self.filters: list[Any] | None = None
        self.events: set[Any] | None = None

    async def async_step_init(self, user_input=None):
        """Step 1 — basic options: prefixes, load_components, service_prefix."""
        if user_input is not None:
            self.options = user_input.copy()
            return await self.async_step_domain_device_entity_filters()

        domains = _local_domains(self.hass)
        domains_with_loaded = sorted(
            set(domains) | set(self.config_entry.options.get(CONF_LOAD_COMPONENTS, []))
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENTITY_PREFIX,
                        description={
                            "suggested_value": self.config_entry.options.get(CONF_ENTITY_PREFIX)
                        },
                    ): str,
                    vol.Optional(
                        CONF_ENTITY_FRIENDLY_NAME_PREFIX,
                        description={
                            "suggested_value": self.config_entry.options.get(
                                CONF_ENTITY_FRIENDLY_NAME_PREFIX
                            )
                        },
                    ): str,
                    vol.Optional(
                        CONF_LOAD_COMPONENTS,
                        default=self.config_entry.options.get(CONF_LOAD_COMPONENTS) or [],
                    ): cv.multi_select(domains_with_loaded),
                    vol.Required(
                        CONF_SERVICE_PREFIX,
                        default=self.config_entry.options.get(CONF_SERVICE_PREFIX)
                        or slugify(self.config_entry.title),
                    ): str,
                }
            ),
        )

    async def async_step_domain_device_entity_filters(self, user_input=None):
        """Step 2 — include/exclude domain, device, and entity filters."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_general_filters()

        domains = _local_domains(self.hass)
        devices = _local_devices(self.hass)
        entities = _local_entities(self.hass)

        # Merge stored IDs that may no longer exist in the live registry,
        # appending them at the end so they remain selectable.
        stored_device_ids = set(
            self.config_entry.options.get(CONF_INCLUDE_DEVICES, [])
        ) | set(self.config_entry.options.get(CONF_EXCLUDE_DEVICES, []))
        all_devices = dict(devices)
        for did in stored_device_ids:
            if did not in all_devices:
                all_devices[did] = did

        stored_entity_ids = set(
            self.config_entry.options.get(CONF_INCLUDE_ENTITIES, [])
        ) | set(self.config_entry.options.get(CONF_EXCLUDE_ENTITIES, []))
        all_entities = dict(entities)
        for eid in sorted(stored_entity_ids):
            if eid not in all_entities:
                all_entities[eid] = eid

        return self.async_show_form(
            step_id="domain_device_entity_filters",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INCLUDE_DOMAINS,
                        default=self.config_entry.options.get(CONF_INCLUDE_DOMAINS) or [],
                    ): cv.multi_select(domains),
                    vol.Optional(
                        CONF_INCLUDE_DEVICES,
                        default=self.config_entry.options.get(CONF_INCLUDE_DEVICES) or [],
                    ): _area_grouped_selector(all_devices),
                    vol.Optional(
                        CONF_INCLUDE_ENTITIES,
                        default=self.config_entry.options.get(CONF_INCLUDE_ENTITIES) or [],
                    ): _area_grouped_selector(all_entities),
                    vol.Optional(
                        CONF_EXCLUDE_DOMAINS,
                        default=self.config_entry.options.get(CONF_EXCLUDE_DOMAINS) or [],
                    ): cv.multi_select(domains),
                    vol.Optional(
                        CONF_EXCLUDE_DEVICES,
                        default=self.config_entry.options.get(CONF_EXCLUDE_DEVICES) or [],
                    ): _area_grouped_selector(all_devices),
                    vol.Optional(
                        CONF_EXCLUDE_ENTITIES,
                        default=self.config_entry.options.get(CONF_EXCLUDE_ENTITIES) or [],
                    ): _area_grouped_selector(all_entities),
                }
            ),
        )

    async def async_step_general_filters(self, user_input=None):
        """Step 3 — numeric range filters."""
        if user_input is not None:
            if CONF_ENTITY_ID not in user_input:
                selected_indices = [
                    int(item.split(".")[0]) - 1
                    for item in user_input.get(CONF_FILTER, [])
                ]
                self.options[CONF_FILTER] = [self.filters[i] for i in selected_indices]
                return await self.async_step_events()

            selected = user_input.get(CONF_FILTER, [])
            new_filter = {conf: user_input.get(conf) for conf in FILTER_OPTIONS}
            selected.append(_filter_str(len(self.filters), new_filter))
            self.filters.append(new_filter)
        else:
            self.filters = self.config_entry.options.get(CONF_FILTER, [])
            selected = [_filter_str(i, f) for i, f in enumerate(self.filters)]

        if self.filters is None:
            self.filters = []
        strings = [_filter_str(i, f) for i, f in enumerate(self.filters)]

        return self.async_show_form(
            step_id="general_filters",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FILTER, default=selected): cv.multi_select(strings),
                    vol.Optional(CONF_ENTITY_ID): str,
                    vol.Optional(CONF_UNIT_OF_MEASUREMENT): str,
                    vol.Optional(CONF_ABOVE): vol.Coerce(float),
                    vol.Optional(CONF_BELOW): vol.Coerce(float),
                }
            ),
        )

    async def async_step_events(self, user_input=None):
        """Step 4 — event subscriptions."""
        if user_input is not None:
            if ADD_NEW_EVENT not in user_input:
                self.options[CONF_SUBSCRIBE_EVENTS] = user_input.get(
                    CONF_SUBSCRIBE_EVENTS, []
                )
                return self.async_create_entry(title="", data=self.options)

            selected = user_input.get(CONF_SUBSCRIBE_EVENTS, [])
            if self.events is None:
                self.events = set()
            self.events.add(user_input[ADD_NEW_EVENT])
            selected.append(user_input[ADD_NEW_EVENT])
        else:
            self.events = set(self.config_entry.options.get(CONF_SUBSCRIBE_EVENTS) or [])
            selected = self.config_entry.options.get(CONF_SUBSCRIBE_EVENTS) or []

        return self.async_show_form(
            step_id="events",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SUBSCRIBE_EVENTS, default=selected
                    ): cv.multi_select(self.events),
                    vol.Optional(ADD_NEW_EVENT): str,
                }
            ),
        )


class RemoteOptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Options flow for remote-mode entries — 1 step (service proxy settings). Reloads entry on save."""

    async def async_step_init(self, user_input=None):
        """Manage remote options: prefixes, service proxy."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Services list requires a live connection — empty if not yet connected
        services: dict[str, str] = {}
        try:
            connection = self.config_entry.runtime_data.connection
            if connection is not None:
                services = {s: s for s in connection.proxy_services.services}
        except AttributeError:
            pass

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENTITY_PREFIX,
                        description={
                            "suggested_value": self.config_entry.options.get(CONF_ENTITY_PREFIX)
                        },
                    ): str,
                    vol.Optional(
                        CONF_ENTITY_FRIENDLY_NAME_PREFIX,
                        description={
                            "suggested_value": self.config_entry.options.get(
                                CONF_ENTITY_FRIENDLY_NAME_PREFIX
                            )
                        },
                    ): str,
                    vol.Required(
                        CONF_SERVICE_PREFIX,
                        default=self.config_entry.options.get(CONF_SERVICE_PREFIX)
                        or slugify(self.config_entry.title),
                    ): str,
                    vol.Optional(
                        CONF_SERVICES,
                        default=self.config_entry.options.get(CONF_SERVICES) or [],
                    ): cv.multi_select(services),
                }
            ),
        )
