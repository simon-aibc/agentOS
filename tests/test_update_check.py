import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from packaging.version import Version

from agent_os.update_check import (
    _clean_version_str,
    _parse_version,
    check_for_update,
)


def test_clean_version_and_parsing():
    assert _clean_version_str("v2.3.0") == "2.3.0"
    assert _clean_version_str("V1.0.0") == "1.0.0"
    assert _clean_version_str("  2.1.0  ") == "2.1.0"
    assert _parse_version("v2.3.0") == Version("2.3.0")
    assert _parse_version("invalid") is None


def test_update_check_newer_version_available(tmp_path: Path):
    cache_file = tmp_path / "cache.json"
    mock_payload = {
        "tag_name": "v2.3.0",
        "html_url": "https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v2.3.0",
        "body": "Release 2.3.0 notes",
    }

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        info = check_for_update(current="2.2.0", cache_path=cache_file, force=True)
        assert info.update_available is True
        assert info.current_version == "2.2.0"
        assert info.latest_version == "2.3.0"
        assert info.release_url == mock_payload["html_url"]
        assert info.release_notes == mock_payload["body"]
        assert mock_urlopen.call_count == 1
        assert cache_file.exists()


def test_update_check_same_or_older_version(tmp_path: Path):
    cache_file = tmp_path / "cache.json"
    mock_payload = {
        "tag_name": "v2.2.0",
        "html_url": "https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v2.2.0",
        "body": "Release 2.2.0 notes",
    }

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        info = check_for_update(current="2.2.0", cache_path=cache_file, force=True)
        assert info.update_available is False
        assert info.latest_version == "2.2.0"


def test_update_check_network_failure_fail_closed(tmp_path: Path):
    cache_file = tmp_path / "cache.json"

    with patch(
        "urllib.request.urlopen", side_effect=urllib.error.URLError("Network down")
    ):
        info = check_for_update(current="2.2.0", cache_path=cache_file, force=True)
        assert info.update_available is False
        assert info.current_version == "2.2.0"
        assert info.latest_version == "2.2.0"
        assert info.error is not None


def test_update_check_cache_hit_avoids_network(tmp_path: Path):
    cache_file = tmp_path / "cache.json"
    # Seed cache
    cached_data = {
        "current_version": "2.2.0",
        "latest_version": "2.3.0",
        "update_available": True,
        "release_url": "https://example.com",
        "release_notes": "Notes",
        "checked_at": "2026-08-20T00:00:00Z",
    }
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")

    with patch("urllib.request.urlopen") as mock_urlopen:
        info = check_for_update(current="2.2.0", cache_path=cache_file, force=False)
        assert info.update_available is True
        assert info.latest_version == "2.3.0"
        assert mock_urlopen.call_count == 0  # No network call!


def test_read_cached_update_missing_cache_no_network(tmp_path: Path):
    cache_file = tmp_path / "nonexistent_cache.json"
    with patch("urllib.request.urlopen") as mock_urlopen:
        info = check_for_update(
            current="2.2.0", cache_path=cache_file, read_cache_only=True
        )
        assert info.update_available is False
        assert info.current_version == "2.2.0"
        assert info.latest_version == "2.2.0"
        assert info.error == "no_cache"
        assert mock_urlopen.call_count == 0


def test_read_cached_update_hit(tmp_path: Path):
    cache_file = tmp_path / "cache.json"
    cached_data = {
        "current_version": "2.2.0",
        "latest_version": "2.3.0",
        "update_available": True,
        "release_url": "https://example.com",
    }
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")
    with patch("urllib.request.urlopen") as mock_urlopen:
        info = check_for_update(
            current="2.2.0", cache_path=cache_file, read_cache_only=True
        )
        assert info.update_available is True
        assert info.latest_version == "2.3.0"
        assert mock_urlopen.call_count == 0
