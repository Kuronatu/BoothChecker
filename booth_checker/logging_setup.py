import logging
from logging.handlers import SysLogHandler
from typing import Optional


def attach_syslog_handler(
    target_logger: logging.Logger,
    syslog_config: dict,
    formatter: Optional[logging.Formatter] = None,
) -> None:
    """Attach a syslog handler to the provided logger when enabled in config."""
    if not syslog_config or not isinstance(syslog_config, dict):
        return

    if not syslog_config.get("enabled"):
        return

    address = syslog_config.get("address")
    if not address:
        target_logger.warning("Syslog logging enabled but no address configured; skipping SysLogHandler setup.")
        return

    port_value = syslog_config.get("port", 514)
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        target_logger.warning("Invalid syslog port '%s'; skipping SysLogHandler setup.", port_value)
        return

    if any(isinstance(handler, SysLogHandler) for handler in target_logger.handlers):
        return

    try:
        syslog_handler = SysLogHandler(address=(address, port))
    except OSError as exc:
        target_logger.error("Failed to initialize SysLogHandler for %s:%s: %s", address, port, exc)
        return

    if formatter:
        syslog_handler.setFormatter(formatter)

    target_logger.addHandler(syslog_handler)
