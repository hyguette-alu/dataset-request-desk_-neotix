"""Structured (JSON-lines) logging.

One line per log record on stdout, so a container log collector can parse it
without a multiline regex. Request logging itself lives in the middleware in
`app.main`; this module only decides the wire format.
"""

import json
import logging
import sys
from datetime import datetime, timezone

# Attributes present on every stdlib LogRecord. Anything else a caller passes
# through `extra=` is application context and belongs in the output.
_STANDARD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def safe_extra(fields: dict) -> dict:
    """Make a dict safe to pass as `extra=` to the logging module.

    stdlib logging raises KeyError if an extra field collides with a name
    LogRecord already uses (`created`, `module`, `name`, `args`, ...). That is
    fine for hand-written call sites, but a crash waiting to happen anywhere a
    dict of application context is splatted in. Colliding keys are suffixed
    rather than dropped, so nothing is silently lost.
    """
    return {
        (f"{key}_value" if key in _STANDARD_ATTRS else key): value
        for key, value in fields.items()
    }


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn ships its own handlers; drop them so everything goes out as JSON.
    # uvicorn.access is disabled outright because our middleware already emits a
    # richer line (it knows the authenticated user, uvicorn does not).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = []
        log.propagate = name != "uvicorn.access"
