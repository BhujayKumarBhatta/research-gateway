from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from research_gateway.config import Settings
from research_gateway.security import redact_text


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [value for value in secrets if value]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self.secrets)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self.secrets)
        return True


class SafeRequestTargetFilter(logging.Filter):
    """Keep request paths and statuses while removing sensitive query strings."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            values = list(record.args)
            if len(values) >= 3:
                values[2] = str(values[2]).partition("?")[0]
                record.args = tuple(values)
        elif (
            record.name in {"httpx", "httpcore"} or record.name.startswith(("httpx.", "httpcore."))
        ) and isinstance(record.args, tuple):
            values = []
            for value in record.args:
                rendered = str(value)
                values.append(rendered.partition("?")[0] if "://" in rendered else value)
            record.args = tuple(values)
        return True


def install_safe_request_target_filters() -> SafeRequestTargetFilter:
    """Protect request diagnostics even when a library owns the console handler."""
    safe_target = SafeRequestTargetFilter()
    for logger_name in ("httpx", "httpx._client", "httpcore"):
        logging.getLogger(logger_name).addFilter(safe_target)
    for handler in logging.getLogger().handlers:
        handler.addFilter(safe_target)
    return safe_target


def configure_logging(settings: Settings) -> Path:
    """Configure one rotating, centrally redacted application log."""
    path = settings.logging.path or settings.database.path.parent / "logs" / "research-gateway.log"
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    redaction = SecretRedactionFilter(settings.secret_values())
    safe_target = install_safe_request_target_filters()
    for existing in list(root.handlers):
        if getattr(existing, "research_gateway_handler", False):
            root.removeHandler(existing)
            existing.close()
        else:
            existing.addFilter(safe_target)
            existing.addFilter(redaction)
    for logger_name in ("httpx", "httpcore", "research_gateway"):
        logger = logging.getLogger(logger_name)
        logger.addFilter(safe_target)
        logger.addFilter(redaction)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.addFilter(safe_target)
        logger.addFilter(redaction)
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    handler.research_gateway_handler = True  # type: ignore[attr-defined]
    handler.addFilter(safe_target)
    handler.addFilter(redaction)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    root.setLevel(getattr(logging, settings.logging.level))
    root.addHandler(handler)
    logging.getLogger("research_gateway").info("Research Gateway logging started.")
    return path
