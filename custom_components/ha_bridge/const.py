"""Constants used by integration."""

CONF_ROLE = "role"
ROLE_HOST = "host"
ROLE_REMOTE = "remote"

CONF_LOAD_COMPONENTS = "load_components"
CONF_SERVICE_PREFIX = "service_prefix"
CONF_SERVICES = "services"

CONF_FILTER = "filter"
CONF_SECURE = "secure"
CONF_SUBSCRIBE_EVENTS = "subscribe_events"
CONF_ENTITY_PREFIX = "entity_prefix"
CONF_ENTITY_FRIENDLY_NAME_PREFIX = "entity_friendly_name_prefix"
CONF_MAX_MSG_SIZE = "max_message_size"

CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_DEVICES = "include_devices"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_EXCLUDE_DEVICES = "exclude_devices"
CONF_EXCLUDE_ENTITIES = "exclude_entities"

WS_CMD_GET_EXPOSED_ENTITIES = "ha_bridge/get_exposed_entities"

STATE_VERSION_MISMATCH = "version_mismatch"
STATE_PENDING_APPROVAL = "pending_approval"

CONF_APPROVED_REMOTES = "approved_remotes"

CONF_HOST_ENTRY_ID = "host_entry_id"

DOMAIN = "ha_bridge"

# replaces 'from homeassistant.core import SERVICE_CALL_LIMIT'
SERVICE_CALL_LIMIT = 10

DEFAULT_MAX_MSG_SIZE = 16 * 1024 * 1024
