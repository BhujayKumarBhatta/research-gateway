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


def configure_logging(settings: Settings) -> Path:
    """Configure one rotating, centrally redacted application log."""
    path = settings.logging.path or settings.database.path.parent / "logs" / "research-gateway.log"
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    redaction = SecretRedactionFilter(settings.secret_values())
    for existing in list(root.handlers):
        if getattr(existing, "research_gateway_handler", False):
            root.removeHandler(existing)
            existing.close()
        else:
            existing.addFilter(redaction)
    for logger_name in ("httpx", "httpcore", "research_gateway"):
        logging.getLogger(logger_name).addFilter(redaction)
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    handler.research_gateway_handler = True  # type: ignore[attr-defined]
    handler.addFilter(redaction)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    root.setLevel(getattr(logging, settings.logging.level))
    root.addHandler(handler)
    logging.getLogger("research_gateway").info("Research Gateway logging started.")
    return path
