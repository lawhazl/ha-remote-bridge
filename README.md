# Home Assistant Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Connect two Home Assistant instances so that entities and devices from one appear live in the other.

---

## What it does

Remote Home-Assistant Bridge links a **host** instance to a **remote** instance over a persistent WebSocket connection. Entities and devices you choose on the host are mirrored onto the remote in real time — state changes appear within seconds, no polling required.

- Mirrors both entities and full device entries, not just states
- Filters are configured on the host — choose exactly which domains, devices, and entities to share
- A connection status sensor on the remote shows the live link state
- Version mismatch between the two instances is detected and blocked automatically

---

## Requirements

- Home Assistant 2024.1 or newer on both instances
- Both instances must run the same version of this integration
- A long-lived access token from the host instance

---

## Installation

1. Open HACS in your Home Assistant
2. Go to **Integrations → Custom repositories**
3. Add `https://github.com/lawhazl/ha-remote-bridge` and select **Integration** as the category
4. Find **Home Assistant Bridge** and install it
5. Restart Home Assistant
6. Repeat on the second instance

---

## Setup

Install the integration on the host instance first, then on the remote.

### Host instance

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Home Assistant Bridge**
3. Select **Host mode**
4. Choose which domains, devices, and entities to expose

### Remote instance

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Home Assistant Bridge**
3. Select **Remote mode**
4. Enter the host address, port, and a long-lived access token from the host
5. The integration verifies the connection before completing — if the versions don't match, setup is blocked until both sides are on the same version

---

## Configuration

### Host options

| Option | Description |
|---|---|
| Include domains | Share all entities in these domains |
| Include devices | Share all entities belonging to these devices |
| Include entities | Share specific entities |
| Exclude domains / devices / entities | Override any inclusion — exclusions always win |
| Entity prefix | Prefix added to all mirrored entity IDs on the remote |
| Friendly name prefix | Prefix added to all mirrored entity names on the remote |
| Unit / range filters | Only share entities matching a unit of measurement or numeric range |
| Event subscriptions | Forward additional event types beyond state changes |

Nothing is shared by default — you opt in explicitly. Exclude rules always override include rules.

### Remote options

| Option | Description |
|---|---|
| Entity prefix | Prefix added to mirrored entity IDs |
| Friendly name prefix | Prefix added to mirrored entity names |
| Service proxy | Select which host services to make callable from the remote |

---

## Connection status

A diagnostic sensor is created on the remote under the bridge device. It reports the current connection state:

| State | Meaning |
|---|---|
| `connecting` | Establishing connection to host |
| `connected` | Link is active and syncing |
| `reconnecting` | Connection lost, retrying |
| `disconnected` | Not connected |
| `auth_invalid` | Access token rejected by host |
| `version_mismatch` | Integration versions differ — update required |

---

## Version compatibility

Both instances must run the same version of this integration. A mismatch is caught at setup time (config flow error) and at runtime (connection is closed, a persistent notification appears on the remote, and the status sensor shows `version_mismatch`). No automatic retry — update the integration on the correct instance and restart.

---

## Diagnostics

Call the `remote_homeassistant.dump_diagnostics` action from **Developer Tools → Actions** on either instance. It writes a structured report to the integration log file covering connection state, active filters, entity and device counts, and any registry entries not in the current exposed list.

Log files are stored at `<config_dir>/custom_components/remote_homeassistant/logs/` and rotated automatically after 7 days.
