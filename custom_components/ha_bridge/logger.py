"""Integration log file manager with 7-day rotation."""
from __future__ import annotations
import asyncio
import concurrent.futures
import logging
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Single-worker executor: guarantees log writes are processed in submission
# order (FIFO queue, one thread) and never block the event loop.
_LOG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="ha_bridge_log"
)

_LOG_FORMAT = "[{timestamp}] [{mode}] [{stage}] {message}"
_DEBUG_FORMAT = "[{timestamp}] [{level}] [{module}] {message}"

# Logger name for this module — used to filter out records that log() already
# writes directly, so we don't get duplicate lines in the file.
_LOGGER_MODULE_NAME = __name__  # "custom_components.ha_bridge.logger"


def _log_dir_from_config(config_dir: str) -> Path:
    return Path(config_dir) / DOMAIN / "logs"


def _log_dir(hass: HomeAssistant) -> Path:
    # Store logs under /config/ha_bridge/logs/ — outside custom_components so
    # HACS updates never wipe them.
    return _log_dir_from_config(hass.config.config_dir)


def _write_line_sync(log_dir: Path, line: str) -> None:
    """Append a single line to today's log file — runs on the executor thread."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{DOMAIN}_{today}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # Can't log an error here without risking recursion


def _write_log_sync(log_dir: Path, mode: str, stage: str, message: str) -> None:
    """Format a structured log entry and write it — runs on the executor thread."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = _LOG_FORMAT.format(timestamp=timestamp, mode=mode, stage=stage, message=message)
    _write_line_sync(log_dir, line)


def log(hass: HomeAssistant, mode: str, stage: str, message: str) -> None:
    """Write a structured log entry to the daily log file and HA debug log.

    Submits to a single-worker ThreadPoolExecutor so writes are always in
    call order and the event loop is never blocked.
    """
    _LOGGER.debug("[%s] [%s] %s", mode, stage, message)
    _LOG_EXECUTOR.submit(_write_log_sync, _log_dir(hass), mode, stage, message)


class _IntegrationFileHandler(logging.Handler):
    """Logging handler that mirrors HA log records into the integration's log file.

    Attached to the root integration logger (custom_components.ha_bridge) so it
    captures every _LOGGER.debug/info/warning/error call across all modules.
    Records emitted by logger.py itself are skipped — those are already written
    to the file in structured form by log() to avoid duplicate lines.
    """

    def __init__(self, config_dir: str) -> None:
        super().__init__(level=logging.DEBUG)
        self._log_dir = _log_dir_from_config(config_dir)

    def emit(self, record: logging.LogRecord) -> None:
        # Skip records from this module — log() already writes them directly.
        if record.name == _LOGGER_MODULE_NAME:
            return
        try:
            module = record.name.rsplit(".", 1)[-1]
            timestamp = datetime.now().isoformat(timespec="seconds")
            line = _DEBUG_FORMAT.format(
                timestamp=timestamp,
                level=record.levelname,
                module=module,
                message=record.getMessage(),
            )
            _LOG_EXECUTOR.submit(_write_line_sync, self._log_dir, line)
        except Exception:
            self.handleError(record)


# Guard so the handler is only added once per HA process lifetime.
_handler_installed = False


def install_file_handler(config_dir: str) -> None:
    """Attach the file handler to the integration logger.

    Must be called once from async_setup. When debug logging is enabled
    for custom_components.ha_bridge, all debug messages will appear in
    the daily log file alongside the structured log() entries.
    """
    global _handler_installed
    if _handler_installed:
        return
    integration_logger = logging.getLogger(f"custom_components.{DOMAIN}")
    integration_logger.addHandler(_IntegrationFileHandler(config_dir))
    _handler_installed = True


def purge_old_logs(hass: HomeAssistant) -> None:
    """Delete log files older than 7 days."""
    try:
        log_dir = _log_dir(hass)
        if not log_dir.exists():
            return
        cutoff = datetime.now() - timedelta(days=7)
        for log_file in log_dir.glob(f"{DOMAIN}_*.log"):
            try:
                date_str = log_file.stem.replace(f"{DOMAIN}_", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    log_file.unlink()
                    _LOGGER.info("Deleted old log file: %s", log_file.name)
            except (ValueError, OSError):
                pass
    except Exception as err:
        _LOGGER.error("Failed to purge old logs: %s", err)


async def async_setup_log_rotation(hass: HomeAssistant) -> None:
    """Purge old logs on startup and schedule daily rotation."""
    await hass.async_add_executor_job(purge_old_logs, hass)

    async def _daily_rotation() -> None:
        while True:
            await asyncio.sleep(86400)
            await hass.async_add_executor_job(purge_old_logs, hass)

    hass.loop.create_task(_daily_rotation())
