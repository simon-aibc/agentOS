import re

import pytest

from agent_os.output_limits import truncate_utf8


def _assert_truncation(original: str, result: str, max_bytes: int) -> None:
    match = re.match(r"^\[truncated (\d+) bytes\]\n", result)
    assert match is not None
    retained = result[match.end() :]
    omitted = len(original.encode("utf-8")) - len(retained.encode("utf-8"))
    assert int(match.group(1)) == omitted
    assert len(result.encode("utf-8")) <= max_bytes


def test_truncate_utf8_preserves_small_value() -> None:
    text = "Hello, world!"

    result, truncated = truncate_utf8(text, 100)

    assert truncated is False
    assert result == text


def test_truncate_utf8_prepends_exact_marker() -> None:
    text = "A" * 1000

    result, truncated = truncate_utf8(text, 100)

    assert truncated is True
    _assert_truncation(text, result, 100)


def test_truncate_utf8_does_not_split_multibyte_character() -> None:
    text = "𐍈" * 100

    result, truncated = truncate_utf8(text, 100)

    assert truncated is True
    assert "�" not in result
    _assert_truncation(text, result, 100)


def test_truncate_utf8_returns_empty_when_marker_cannot_fit() -> None:
    result, truncated = truncate_utf8("A" * 100, 5)

    assert truncated is True
    assert result == ""


def test_truncate_utf8_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        truncate_utf8("value", -1)
