import logging
import logging.handlers
import os
import re

from pythonjsonlogger import jsonlogger

_SECRET_KEYS = ("client_secret", "app_password", "authorization", "password")
_REDACT_RE = re.compile(
    r'(' + "|".join(_SECRET_KEYS) + r')(["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)',
    re.IGNORECASE,
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _REDACT_RE.sub(r"\1\2***REDACTED***", record.msg)
        if record.args:
            record.args = tuple(
                _REDACT_RE.sub(r"\1\2***REDACTED***", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        for key in list(record.__dict__.keys()):
            if key.lower() in _SECRET_KEYS:
                record.__dict__[key] = "***REDACTED***"
        return True


def configure_logging(log_dir: str, log_level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)
    formatter = jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretRedactionFilter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers = [file_handler, console_handler]
