import logging
import logging.handlers
import os
import re

from pythonjsonlogger import json as jsonlogger

_SECRET_KEYS = ("client_secret", "app_password", "authorization", "password")
_KEY_ALT = "|".join(re.escape(key) for key in _SECRET_KEYS)

# Redaction runs on the FULLY FORMATTED output (after %-style message
# interpolation and JSON serialization have already happened), not on
# record.msg / record.args. Operating on the final text means:
#   - a secret value is bounded by real delimiters (a JSON quote, a comma,
#     a closing brace) or by the start of the next known secret key, never
#     by whitespace -- so values with internal spaces (e.g. Google/Mailcow
#     app passwords like "abcd efgh ijkl mnop") are redacted in full.
#   - non-string args (dicts, objects) are already stringified by the time
#     we see them, so there's nothing to special-case by type.
#   - record.msg/record.args are never mutated, so a "%s"/"%(name)s"
#     placeholder can never be corrupted into invalid format syntax.
_REDACT_RE = re.compile(
    r"\b(" + _KEY_ALT + r")\b"
    r"""(['"]?\s*[:=]\s*['"]?)"""
    r"""(.+?)(?=['",}]|\s+(?:""" + _KEY_ALT + r""")\b|$)""",
    re.IGNORECASE,
)


def _redact_text(text: str) -> str:
    return _REDACT_RE.sub(r"\1\2***REDACTED***", text)


class RedactingFormatter(jsonlogger.JsonFormatter):
    """JSON formatter that redacts secret key/value pairs from its own
    fully-formatted output, after message interpolation and JSON
    serialization are complete."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return _redact_text(formatted)


class SecretRedactionFilter(logging.Filter):
    """Defense-in-depth filter that redacts secret-named *extra* record
    attributes (e.g. ``logger.info(..., extra={"password": "x"})``).

    This filter intentionally does NOT touch ``record.msg`` or
    ``record.args``: mutating either before Python's own %-style
    formatting runs risks corrupting placeholders (a %s/%(name)s
    substitution) or mishandling non-string/mapping args, which was the
    root cause of the redaction gaps found in the previous version. The
    required, complete redaction pass is ``RedactingFormatter``, which
    operates on the final formatted text; this filter only adds a second,
    narrowly-scoped safety net that cannot corrupt formatting because it
    never changes msg/args.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__.keys()):
            if key.lower() in _SECRET_KEYS:
                record.__dict__[key] = "***REDACTED***"
        return True


def configure_logging(log_dir: str, log_level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)
    formatter = RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")

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

    for old_handler in root.handlers:
        old_handler.close()
    root.handlers = [file_handler, console_handler]
