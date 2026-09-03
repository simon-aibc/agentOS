"""Update discovery and version check against GitHub Releases with 24h caching.

Fail-closed: Network failures, timeouts, and parse errors always return
update_available=False and never block or raise.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from agent_os.server.runtime import package_version

logger = logging.getLogger(__name__)

DEFAULT_RELEASES_URL = (
    "https://api.github.com/repos/simon-aibc/agent-os-langgraph/releases/latest"
)
CACHE_TTL_SECONDS = 86400  # 24 hours


@dataclass
class UpdateInfo:
    """Version and update availability details."""

    current_version: str
    latest_version: str
    update_available: bool
    release_url: str | None = None
    release_notes: str | None = None
    checked_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_cache_path(custom_path: Path | str | None = None) -> Path:
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    env_path = os.getenv("AGENT_OS_UPDATE_CACHE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".agent_os" / "update_cache.json"


def _clean_version_str(ver_str: str) -> str:
    """Normalize version string by removing 'v' prefix and trimming."""
    return re.sub(r"^[vV]", "", ver_str.strip())


def _parse_version(ver_str: str) -> Version | None:
    try:
        return Version(_clean_version_str(ver_str))
    except InvalidVersion:
        return None


def _read_cache(cache_path: Path, current_version: str) -> UpdateInfo | None:
    try:
        if not cache_path.exists() or not cache_path.is_file():
            return None

        # Check file age
        mtime = cache_path.stat().st_mtime
        if time.time() - mtime > CACHE_TTL_SECONDS:
            return None

        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)

        cached_info = UpdateInfo(
            current_version=current_version,
            latest_version=data.get("latest_version", current_version),
            update_available=bool(data.get("update_available", False)),
            release_url=data.get("release_url"),
            release_notes=data.get("release_notes"),
            checked_at=data.get("checked_at"),
            error=data.get("error"),
        )
        # Re-evaluate update_available against the requested current_version
        curr_v = _parse_version(current_version)
        latest_v = _parse_version(cached_info.latest_version)
        if curr_v and latest_v:
            cached_info.update_available = latest_v > curr_v

        return cached_info
    except Exception as exc:
        logger.debug("Failed to read update cache from %s: %s", cache_path, exc)
        return None


def _write_cache(cache_path: Path, info: UpdateInfo) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(info.to_dict(), f, indent=2)
    except Exception as exc:
        logger.debug("Failed to write update cache to %s: %s", cache_path, exc)


def read_cached_update(
    current: str | None = None,
    *,
    cache_path: Path | str | None = None,
) -> UpdateInfo:
    """Read cached update info without making any network requests.

    If cache is missing, unreadable, or expired, returns a safe UpdateInfo with
    update_available=False and error="no_cache".
    """
    current_ver = current or package_version()
    resolved_cache = _get_cache_path(cache_path)
    cached = _read_cache(resolved_cache, current_ver)
    if cached is not None:
        return cached
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    return UpdateInfo(
        current_version=current_ver,
        latest_version=current_ver,
        update_available=False,
        checked_at=now_iso,
        error="no_cache",
    )


def check_for_update(
    current: str | None = None,
    *,
    cache_path: Path | str | None = None,
    force: bool = False,
    timeout: float = 3.0,
    read_cache_only: bool = False,
) -> UpdateInfo:
    """Check for new release against GitHub Releases.

    - Cache-first (24h TTL).
    - Fail-closed: returns update_available=False on any failure without raising.
    """
    if read_cache_only:
        return read_cached_update(current, cache_path=cache_path)

    current_ver = current or package_version()
    resolved_cache = _get_cache_path(cache_path)

    if not force:
        cached = _read_cache(resolved_cache, current_ver)
        if cached is not None:
            return cached

    releases_url = os.getenv("AGENT_OS_RELEASES_URL", DEFAULT_RELEASES_URL)
    now_iso = dt.datetime.now(dt.UTC).isoformat()

    try:
        req = urllib.request.Request(
            releases_url,
            headers={
                "User-Agent": f"AgentOS/{current_ver}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return UpdateInfo(
                    current_version=current_ver,
                    latest_version=current_ver,
                    update_available=False,
                    checked_at=now_iso,
                    error=f"HTTP {response.status}",
                )
            payload = json.loads(response.read().decode("utf-8"))

        raw_tag = payload.get("tag_name", "")
        latest_ver_clean = _clean_version_str(raw_tag)
        release_url = payload.get("html_url")
        release_notes = payload.get("body")

        curr_v = _parse_version(current_ver)
        latest_v = _parse_version(latest_ver_clean)

        update_available = bool(curr_v and latest_v and (latest_v > curr_v))

        info = UpdateInfo(
            current_version=current_ver,
            latest_version=latest_ver_clean or current_ver,
            update_available=update_available,
            release_url=release_url,
            release_notes=release_notes,
            checked_at=now_iso,
        )
        _write_cache(resolved_cache, info)
        return info
    except urllib.error.HTTPError as error:
        try:
            logger.debug("Update check failed (safe fallback): %s", error)
            return UpdateInfo(
                current_version=current_ver,
                latest_version=current_ver,
                update_available=False,
                checked_at=now_iso,
                error=str(error),
            )
        finally:
            error.close()
    except Exception as exc:
        logger.debug("Update check failed (safe fallback): %s", exc)
        return UpdateInfo(
            current_version=current_ver,
            latest_version=current_ver,
            update_available=False,
            checked_at=now_iso,
            error=str(exc),
        )
