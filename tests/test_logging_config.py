import logging
import os

from app.logging_config import configure_logging


def test_secret_fields_are_redacted_in_log_file(tmp_path):
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction")
    logger.info("connecting with client_secret=abc123XYZ and app_password=hunter2hunter2")

    log_file = os.path.join(log_dir, "app.log")
    with open(log_file, encoding="utf-8") as f:
        content = f.read()

    assert "abc123XYZ" not in content
    assert "hunter2hunter2" not in content
    assert "REDACTED" in content
