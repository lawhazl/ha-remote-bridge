"""Sensor platform for connection status."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import (
    CONF_ENTITY_PREFIX,
    CONF_ENTITY_FRIENDLY_NAME_PREFIX,
    CONF_MAX_MSG_SIZE,
    CONF_SECURE,
    DEFAULT_MAX_MSG_SIZE,
    DOMAIN,
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up sensor based on config entry."""
    async_add_entities([ConnectionStatusSensor(config_entry)])


class ConnectionStatusSensor(SensorEntity):
    """Representation of a Remote Bridge connection status sensor."""

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, config_entry):
        """Initialize the sensor."""
        self._native_value = None
        self._entry = config_entry

        proto = "https" if config_entry.data.get(CONF_SECURE) else "http"
        host = config_entry.data[CONF_HOST]
        port = config_entry.data[CONF_PORT]
        self._attr_unique_id = config_entry.unique_id
        self._attr_device_info = DeviceInfo(
            name="Home Assistant",
            configuration_url=f"{proto}://{host}:{port}",
            identifiers={(DOMAIN, f"remote_{self._attr_unique_id}")},
        )

    @property
    def native_value(self):
        """Return the current connection state."""
        return self._native_value

    @property
    def extra_state_attributes(self):
        """Return device state attributes."""
        return {
            "host": self._entry.data[CONF_HOST],
            "port": self._entry.data[CONF_PORT],
            "secure": self._entry.data.get(CONF_SECURE, False),
            "verify_ssl": self._entry.data.get(CONF_VERIFY_SSL, False),
            "max_msg_size": self._entry.data.get(CONF_MAX_MSG_SIZE, DEFAULT_MAX_MSG_SIZE),
            "entity_prefix": self._entry.options.get(CONF_ENTITY_PREFIX, ""),
            "entity_friendly_name_prefix": self._entry.options.get(
                CONF_ENTITY_FRIENDLY_NAME_PREFIX, ""
            ),
            "uuid": self.unique_id,
        }

    async def async_added_to_hass(self):
        """Subscribe to connection state updates."""
        await super().async_added_to_hass()

        def _update_handler(state):
            self._native_value = state
            self.async_write_ha_state()

        signal = f"ha_bridge_{self._entry.unique_id}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, _update_handler)
        )
