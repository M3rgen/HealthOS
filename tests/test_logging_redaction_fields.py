"""What the log filter guarantees for a record, read from the record it leaves behind."""

from __future__ import annotations

import json
import logging
import traceback

import pytest

from app.core.logging import RedactSecrets, configure_logging, error_frames


def record_with(**fields) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "synthetic", (), None)
    for name, value in fields.items():
        setattr(record, name, value)
    return record


# refresh_token has its own regression next to the OAuth redaction.
@pytest.mark.parametrize("name", ["access_token", "client_secret", "authorization", "healthos_ui"])
def test_every_credential_field_is_replaced(name):
    record = record_with(**{name: "secret-value"})
    RedactSecrets().filter(record)
    assert getattr(record, name) == "[REDACTED]"
    assert "secret-value" not in str(record.__dict__)


def test_mixed_case_credentials_are_redacted_in_json_output(capsys):
    configure_logging("INFO")
    logging.getLogger(__name__).info(
        "synthetic", extra={"Authorization": "Bearer s3cr3t", "REFRESH_TOKEN": "x"}
    )
    output = capsys.readouterr().out
    fields = json.loads(output)
    assert fields["Authorization"] == "[REDACTED]"
    assert fields["REFRESH_TOKEN"] == "[REDACTED]"
    assert "s3cr3t" not in output
    assert '"x"' not in output


def test_a_cached_traceback_rendering_does_not_survive():
    record = record_with()
    record.exc_text = "Traceback ... synthetic-secret"
    RedactSecrets().filter(record)
    assert record.exc_text is None


def test_configured_logging_silences_transport_info_with_urls():
    # The test baseline already quiets them: start from loud to see configure_logging act.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.INFO)
    configure_logging("INFO")
    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_frames_name_only_the_file_for_foreign_code(tmp_path):
    module = tmp_path / "foreign_plugin.py"
    module.write_text("def boom():\n    raise ValueError('synthetic')\n", encoding="utf-8")
    namespace: dict = {}
    exec(compile(module.read_text("utf-8"), str(module), "exec"), namespace)
    try:
        namespace["boom"]()
    except ValueError as exc:
        where = error_frames(exc)
        trace = "".join(traceback.format_exception(exc))
    assert str(module) in trace  # the raw traceback has the path; the frames must not
    assert str(tmp_path) not in where
    # The frame names the module alone, without the directories it came from.
    assert where.split(" > ")[-1].split(":")[0] == "foreign_plugin"
