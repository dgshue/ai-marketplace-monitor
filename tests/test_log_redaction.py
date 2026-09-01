"""The on-disk log must never carry credentials, whatever logs them."""

import logging

from ai_marketplace_monitor.webui.log_handler import SecretRedactingFilter, _redact


def test_password_in_dataclass_repr_is_redacted() -> None:
    line = "Running job Job(args=(FacebookMarketplaceConfig(name='facebook', password='Hunter2!Str0ng', username='me@x.com'),))"
    out = _redact(line)
    assert "Hunter2!Str0ng" not in out
    assert "password=***REDACTED***" in out
    # unrelated fields are untouched
    assert "name='facebook'" in out


def test_password_with_double_quotes_and_symbols() -> None:
    assert "p@ss w0rd" not in _redact('password="p@ss w0rd"')


def test_filter_rewrites_record_before_formatting() -> None:
    record = logging.LogRecord(
        "x", logging.DEBUG, __file__, 1, "cfg %s", ("password='abc123'",), None
    )
    assert SecretRedactingFilter().filter(record) is True
    assert "abc123" not in logging.Formatter("%(message)s").format(record)


def test_filter_leaves_clean_records_alone() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "found %d listings", (3,), None)
    SecretRedactingFilter().filter(record)
    assert record.args == (3,)
    assert logging.Formatter("%(message)s").format(record) == "found 3 listings"
