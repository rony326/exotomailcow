import logging
import os

from app.logging_config import configure_logging


def _read_log(log_dir):
    log_file = os.path.join(log_dir, "app.log")
    with open(log_file, encoding="utf-8") as f:
        return f.read()


def test_secret_fields_are_redacted_in_log_file(tmp_path):
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction")
    logger.info("connecting with client_secret=abc123XYZ and app_password=hunter2hunter2")

    content = _read_log(log_dir)

    assert "abc123XYZ" not in content
    assert "hunter2hunter2" not in content
    assert "REDACTED" in content


def test_secret_value_with_internal_spaces_is_fully_redacted(tmp_path):
    # Finding 1: Google/Mailcow-style app passwords are canonically
    # formatted with internal spaces, e.g. "abcd efgh ijkl mnop". A
    # regex that stops the value capture at the first whitespace would
    # only redact "abcd", leaking the remaining 12 characters.
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction-spaces")
    logger.info("app_password=abcd efgh ijkl mnop")

    content = _read_log(log_dir)

    assert "abcd" not in content
    assert "efgh" not in content
    assert "ijkl" not in content
    assert "mnop" not in content
    assert "REDACTED" in content


def test_dict_arg_with_secret_key_is_redacted(tmp_path, capsys):
    # Finding 2: non-string record.args (e.g. a dict) previously bypassed
    # the filter entirely and got fully stringified with real secret
    # values by the formatter at emit time.
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction-dict-arg")
    resp = {"client_secret": "topsecretdictvalue", "ok": True}
    logger.info("resp: %s", resp)

    content = _read_log(log_dir)
    captured = capsys.readouterr()

    assert "topsecretdictvalue" not in content
    assert "REDACTED" in content
    # No corrupted-formatting output should have leaked to stderr either.
    assert "topsecretdictvalue" not in captured.err


def test_percent_s_placeholder_with_plain_secret_arg_is_redacted(tmp_path, capsys):
    # Finding 3: redacting record.msg before formatting can corrupt a
    # "%s" placeholder (msg becomes "client_secret=%s", which the old
    # regex would itself match as the "value" and destroy, while
    # record.args still held the raw secret). That raised a TypeError
    # during %-formatting, which logging.Handler.handleError catches and
    # writes "Message: %r\nArguments: %s" straight to stderr -- printing
    # the secret in plaintext. Assert this doesn't happen.
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction-percent-s")
    logger.info("client_secret=%s", "plainsecretvalue123")

    content = _read_log(log_dir)
    captured = capsys.readouterr()

    assert "plainsecretvalue123" not in content
    assert "REDACTED" in content
    assert "plainsecretvalue123" not in captured.err
    assert "Traceback" not in captured.err
    assert "not all arguments converted" not in captured.err


def test_mapping_style_args_still_format_correctly_and_redact(tmp_path, capsys):
    # Finding 4: a mapping-style record.args (a single dict, valid for
    # "%(name)s"-style formatting) previously got iterated as its *keys*
    # by the args-handling loop, replacing the mapping with a tuple of
    # key names and breaking that valid formatting pattern. Verify both
    # that formatting still works and that the secret value is redacted.
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction-mapping-args")
    logger.info(
        "user=%(user)s client_secret=%(client_secret)s",
        {"user": "bob", "client_secret": "mapsecretvalue"},
    )

    content = _read_log(log_dir)
    captured = capsys.readouterr()

    assert "user=bob" in content
    assert "mapsecretvalue" not in content
    assert "REDACTED" in content
    assert "mapsecretvalue" not in captured.err
    assert "Traceback" not in captured.err


def test_extra_secret_attribute_is_redacted(tmp_path):
    # Defense-in-depth: a secret-named attribute passed via extra= is
    # redacted whether the JSON formatter's own redaction pass or the
    # SecretRedactionFilter catches it.
    log_dir = str(tmp_path / "logs")
    configure_logging(log_dir, "INFO")

    logger = logging.getLogger("test-redaction-extra")
    logger.info("connecting", extra={"password": "extrasecretvalue"})

    content = _read_log(log_dir)

    assert "extrasecretvalue" not in content
    assert "REDACTED" in content
