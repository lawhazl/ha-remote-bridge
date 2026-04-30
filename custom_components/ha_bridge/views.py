"""HTTP views for Remote Home-Assistant."""
from __future__ import annotations
import json
from pathlib import Path

import homeassistant
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.system_info import async_get_system_info
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DEVICES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_ROLE,
    DOMAIN,
    ROLE_HOST,
)

ATTR_INSTALLATION_TYPE = "installation_type"


def _load_integration_version() -> str:
    """Read integration version from manifest.json at import time (sync is safe here)."""
    manifest = Path(__file__).parent / "manifest.json"
    try:
        with open(manifest, encoding="utf-8") as f:
            return json.load(f).get("version", "unknown")
    except Exception:
        return "unknown"


# Cached at import time — never called inside the event loop
_INTEGRATION_VERSION: str = _load_integration_version()


def get_integration_version() -> str:
    """Return cached integration version."""
    return _INTEGRATION_VERSION


class DiscoveryInfoView(HomeAssistantView):
    """Discovery endpoint for Remote Bridge host instances."""

    url = "/api/ha_bridge/discovery"
    name = "api:ha_bridge:discovery"
    requires_auth = False

    async def get(self, request):
        """Get discovery information."""
        hass = request.app["hass"]
        system_info = await async_get_system_info(hass)
        return self.json(
            {
                "uuid": await async_get_instance_id(hass),
                "location_name": hass.config.location_name,
                "ha_version": homeassistant.const.__version__,
                "installation_type": system_info[ATTR_INSTALLATION_TYPE],
                "integration_version": get_integration_version(),
            }
        )


class HostConfigsView(HomeAssistantView):
    """Authenticated endpoint that lists host-mode config entries."""

    url = "/api/ha_bridge/host_configs"
    name = "api:ha_bridge:host_configs"
    requires_auth = True

    async def get(self, request):
        """Return a list of host-mode config entries with their filter summaries."""
        hass = request.app["hass"]
        result = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ROLE) != ROLE_HOST:
                continue
            opts = entry.options
            result.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "include_domains": opts.get(CONF_INCLUDE_DOMAINS, []),
                    "include_devices_count": len(opts.get(CONF_INCLUDE_DEVICES, [])),
                    "include_entities_count": len(opts.get(CONF_INCLUDE_ENTITIES, [])),
                    "exclude_domains": opts.get(CONF_EXCLUDE_DOMAINS, []),
                    "exclude_devices_count": len(opts.get(CONF_EXCLUDE_DEVICES, [])),
                    "exclude_entities_count": len(opts.get(CONF_EXCLUDE_ENTITIES, [])),
                }
            )
        return self.json(result)
