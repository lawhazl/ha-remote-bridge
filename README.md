# Remote Home-Assistant Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration that bridges two HA instances — mirroring entities and devices from a **host** instance onto a **remote** instance in real time.

## Features

- **Two-mode setup** — install on both HA instances; choose Host or Remote mode during setup
- **Device mirroring** — mirrors devices (not just entities) from host to remote, with full registry support
- **Granular filtering** — include/exclude by domain, device, or entity on the host side
- **Version enforcement** — connection blocked if integration versions don't match on both sides
- **Connection status sensor** — diagnostic entity showing live connection state
- **Structured diagnostics** — `dump_diagnostics` action with stage-tagged log output
- **Zeroconf discovery** — host instances are auto-discovered on the remote's network

## Requirements

- Home Assistant 2024.1 or newer on both instances
- Both instances must be on the same version of this integration

## Installation via HACS

1. Open HACS in your Home Assistant
2. Go to **Integrations** → **Custom repositories**
3. Add `https://github.com/lawhazl/ha-remote-bridge` as an **Integration**
4. Install **Remote Home-Assistant Bridge**
5. Restart Home Assistant

## Setup

### Host instance (install first)

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Remote Home-Assistant**
3. Choose **Host mode**
4. Configure which domains, devices, and entities to expose to remotes

### Remote instance (install second)

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Remote Home-Assistant**
3. Choose **Remote mode**
4. Enter the host's address, port, and long-lived access token
5. The integration verifies the connection and version before completing setup

## Architecture

```
Host HA ──(WebSocket)──► Remote HA
  filters entities/devices      mirrors them locally
  responds to WS queries        reconciles on reconnect
  registers discovery view      shows connection status sensor
```

The remote instance opens a persistent WebSocket connection to the host. On connect it fetches a filtered entity and device snapshot, reconciles its local registries, then subscribes to live state updates.

## Configuration options

### Host mode

- Include/exclude by domain, device, or entity
- Entity prefix and friendly name prefix
- General filters (unit of measurement, above/below thresholds)
- Additional event subscriptions

### Remote mode

- Entity prefix and friendly name prefix
- Service proxy — select which host services to proxy locally

## Diagnostics

Call the `remote_homeassistant.dump_diagnostics` action from **Developer Tools → Actions** on either instance to write a full structured diagnostic report to the integration log file.

Log files are written to `<config_dir>/custom_components/remote_homeassistant/logs/` and rotated automatically after 7 days.

## Version compatibility

Both instances must run the same version of this integration. A mismatch is detected at setup time (blocked in config flow) and at runtime (connection closed, persistent notification created, status sensor shows `version_mismatch`).
