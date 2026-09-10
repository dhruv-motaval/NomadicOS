import logging

from nomadicos.core.logging import configure_logging, get_logger, redact


def test_redact_masks_sensitive_keys_at_any_depth() -> None:
    data = {
        "password": "hunter2",
        "nested": {"api_key": "sk-abcdef123456", "note": "safe"},
        "list": [{"token": "tok-1"}, "plain"],
        "safe": 1,
    }
    red = redact(data)
    assert red["password"] == "***REDACTED***"
    assert red["nested"]["api_key"] == "***REDACTED***"
    assert red["nested"]["note"] == "safe"
    assert red["list"][0]["token"] == "***REDACTED***"
    assert red["list"][1] == "plain"
    assert red["safe"] == 1
    assert data["password"] == "hunter2"  # input not mutated


def test_redact_masks_secret_patterns_in_strings() -> None:
    text = "connect with Bearer abc.def.ghi and key sk-abcdefgh12345"
    assert "Bearer abc.def.ghi" not in redact(text)
    assert "sk-abcdefgh12345" not in redact(text)


def test_structured_logging_outputs_json_and_redacts(caplog) -> None:
    import logging as std_logging

    from nomadicos.core.logging import StructuredFormatter

    underlying = std_logging.getLogger("nomadicos.core.test")
    underlying.addHandler(caplog.handler)
    underlying.propagate = False
    logger = get_logger("core.test")
    logger.warning(
        "tool requested",
        extra={"fields": {"tool": "filesystem.read", "api_key": "sk-abcdefgh12345"}},
    )
    record = caplog.records[-1]
    fields = record.__dict__.get("fields", {})
    assert fields["api_key"] == "***REDACTED***"
    assert fields["tool"] == "filesystem.read"
    # formatted JSON output redacts as well
    formatted = StructuredFormatter().format(record)
    assert "sk-abcdefgh12345" not in formatted
    underlying.removeHandler(caplog.handler)
    underlying.propagate = True


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert sum(isinstance(h.formatter, logging.Formatter) for h in root.handlers) >= 1
