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


def _log_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.config_dir) / "custom_components" / DOMAIN / "logs"


def _write_log_sync(log_dir: Path, mode: str, stage: str, message: str) -> None:
    """Blocking file write — runs on the dedicated log executor thread."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{DOMAIN}_{today}.log"
        timestamp = datetime.now().isoformat(timespec="seconds")
        line = _LOG_FORMAT.format(timestamp=timestamp, mode=mode, stage=stage, message=message)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as err:
        _LOGGER.error("Failed to write to integration log: %s", err)


def log(hass: HomeAssistant, mode: str, stage: str, message: str) -> None:
    """Write a structured log entry to the daily log file and HA debug log.

    Submits to a single-worker ThreadPoolExecutor so writes are always in
    call order and the event loop is never blocked.
    """
    _LOGGER.debug("[%s] [%s] %s", mode, stage, message)
    _LOG_EXECUTOR.submit(_write_log_sync, _log_dir(hass), mode, stage, message)


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
