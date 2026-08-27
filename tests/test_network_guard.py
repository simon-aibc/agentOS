import socket
import sys
import urllib.error
import urllib.request

import conftest as active_conftest
import pytest

sys.modules.setdefault("tests.conftest", active_conftest)
from tests.conftest import NetworkBlockedError  # noqa: E402

NETWORK_BLOCKED_ERRORS = (NetworkBlockedError,)


def _has_network_blocked_error(exc: BaseException) -> bool:
    pending = [exc]
    seen: set[int] = set()

    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, NETWORK_BLOCKED_ERRORS):
            return True

        for linked in (
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
        ):
            if isinstance(linked, BaseException):
                pending.append(linked)

    return False


def _closed_loopback_port() -> int:
    return 1


def test_create_connection_to_non_loopback_host_is_blocked():
    host = "example.com"

    with pytest.raises(NETWORK_BLOCKED_ERRORS) as exc_info:
        socket.create_connection((host, 443), timeout=0.01)

    message = str(exc_info.value)
    assert host in message
    assert "@pytest.mark.network" in message


def test_getaddrinfo_for_non_loopback_hostname_is_blocked():
    with pytest.raises(NETWORK_BLOCKED_ERRORS):
        socket.getaddrinfo("example.com", 443)


def test_getaddrinfo_for_ip_literal_is_allowed():
    results = socket.getaddrinfo("93.184.216.34", 80)

    assert results


def test_loopback_connection_is_not_blocked_by_guard():
    port = _closed_loopback_port()

    with pytest.raises(OSError) as exc_info:
        socket.create_connection(("127.0.0.1", port), timeout=0.2)

    assert not isinstance(exc_info.value, NETWORK_BLOCKED_ERRORS)


def test_urllib_urlopen_to_external_host_is_blocked():
    with pytest.raises((*NETWORK_BLOCKED_ERRORS, urllib.error.URLError)) as exc_info:
        urllib.request.urlopen("https://example.com", timeout=0.01)

    assert _has_network_blocked_error(exc_info.value)


@pytest.mark.network
def test_network_marker_allows_guarded_path_without_outbound_call():
    assert active_conftest._network_allowed is True

    results = socket.getaddrinfo("93.184.216.34", 80)

    assert results
