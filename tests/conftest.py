import ipaddress
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

CHECKPOINT_DB_ENV = "AGENT_OS_CHECKPOINTS_DB"

_original_checkpoint_path = os.environ.get(CHECKPOINT_DB_ENV)
_checkpoint_directory = TemporaryDirectory(prefix="agent-os-pytest-")
_original_architect = os.environ.get("LLM_ARCHITECT")
_original_executor = os.environ.get("LLM_EXECUTOR")

os.environ["LLM_ARCHITECT"] = "offline/architect"
os.environ["LLM_EXECUTOR"] = "offline/executor"
os.environ[CHECKPOINT_DB_ENV] = str(Path(_checkpoint_directory.name) / "checkpoints.db")


class NetworkBlockedError(RuntimeError):
    """Raised when an outbound network call is attempted during offline tests."""


_network_allowed: bool = False

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_create_connection = socket.create_connection
_orig_sendto = socket.socket.sendto
_orig_getaddrinfo = socket.getaddrinfo
_orig_gethostbyname = socket.gethostbyname
_orig_gethostbyname_ex = socket.gethostbyname_ex


def _normalize_host(host: Any) -> str | None:
    if host is None:
        return None
    if isinstance(host, (bytes, bytearray)):
        return host.decode("idna", errors="ignore")
    if isinstance(host, int):
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return str(host)
    return str(host)


def _is_loopback_host(host: Any) -> bool:
    host_str = _normalize_host(host)
    if host_str is None or host_str == "":
        return True
    if host_str.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host_str)
        return bool(ip.is_loopback or ip.is_unspecified)
    except ValueError:
        return False


def _check_socket_address(address: Any) -> None:
    if _network_allowed:
        return
    if isinstance(address, tuple) and len(address) >= 1:
        host = address[0]
        port = address[1] if len(address) > 1 else None
        if not _is_loopback_host(host):
            target = f"{host}:{port}" if port is not None else str(host)
            raise NetworkBlockedError(
                f"Outbound network access to '{target}' is blocked during offline tests. "
                f"Mock the request or mark the test with '@pytest.mark.network' to allow real network access."
            )


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check_socket_address(address)
    return _orig_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check_socket_address(address)
    return _orig_connect_ex(self, address)


def _guarded_create_connection(
    address: Any,
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: Any = None,
    *,
    all_errors: bool = False,
) -> socket.socket:
    _check_socket_address(address)
    return _orig_create_connection(
        address,
        timeout=timeout,
        source_address=source_address,
        all_errors=all_errors,
    )


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
    if args:
        target_addr = args[1] if len(args) > 1 else args[0]
        _check_socket_address(target_addr)
    return _orig_sendto(self, data, *args)


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    if _network_allowed:
        return _orig_getaddrinfo(host, port, *args, **kwargs)

    host_str = _normalize_host(host)
    if host_str is None or host_str == "":
        return _orig_getaddrinfo(host, port, *args, **kwargs)

    if host_str.lower() in ("localhost", "localhost.localdomain"):
        return _orig_getaddrinfo(host, port, *args, **kwargs)

    try:
        ipaddress.ip_address(host_str)
        # IP literals do not trigger network DNS queries; libc converts them locally.
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    except ValueError:
        pass

    target = f"{host_str}:{port}" if port is not None else host_str
    raise NetworkBlockedError(
        f"Outbound network DNS resolution for '{target}' is blocked during offline tests. "
        f"Mock socket.getaddrinfo or mark the test with '@pytest.mark.network' to allow real network access."
    )


def _guarded_gethostbyname(hostname: Any) -> str:
    if _network_allowed:
        return _orig_gethostbyname(hostname)
    host_str = _normalize_host(hostname)
    if host_str and host_str.lower() in ("localhost", "localhost.localdomain"):
        return _orig_gethostbyname(hostname)
    try:
        if host_str:
            ipaddress.ip_address(host_str)
            return _orig_gethostbyname(hostname)
    except ValueError:
        pass
    raise NetworkBlockedError(
        f"Outbound network DNS resolution for '{hostname}' is blocked during offline tests. "
        f"Mock the DNS/network call or mark the test with '@pytest.mark.network' to allow real network access."
    )


def _guarded_gethostbyname_ex(hostname: Any) -> Any:
    if _network_allowed:
        return _orig_gethostbyname_ex(hostname)
    host_str = _normalize_host(hostname)
    if host_str and host_str.lower() in ("localhost", "localhost.localdomain"):
        return _orig_gethostbyname_ex(hostname)
    try:
        if host_str:
            ipaddress.ip_address(host_str)
            return _orig_gethostbyname_ex(hostname)
    except ValueError:
        pass
    raise NetworkBlockedError(
        f"Outbound network DNS resolution for '{hostname}' is blocked during offline tests. "
        f"Mock the DNS/network call or mark the test with '@pytest.mark.network' to allow real network access."
    )


def _apply_network_guard() -> None:
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
    socket.socket.sendto = _guarded_sendto
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex


def _restore_network_guard() -> None:
    socket.socket.connect = _orig_connect
    socket.socket.connect_ex = _orig_connect_ex
    socket.create_connection = _orig_create_connection
    socket.socket.sendto = _orig_sendto
    socket.getaddrinfo = _orig_getaddrinfo
    socket.gethostbyname = _orig_gethostbyname
    socket.gethostbyname_ex = _orig_gethostbyname_ex


_apply_network_guard()


@pytest.fixture(scope="session", autouse=True)
def block_outbound_network():
    """Block real outbound network connections for the entire test session."""
    _apply_network_guard()
    yield
    _restore_network_guard()


@pytest.fixture(autouse=True)
def _network_guard_per_test(request: pytest.FixtureRequest):
    """Enable real network access if test is marked with @pytest.mark.network."""
    global _network_allowed
    if request.node.get_closest_marker("network"):
        _network_allowed = True
        try:
            yield
        finally:
            _network_allowed = False
    else:
        yield


@pytest.fixture(scope="session", autouse=True)
def isolate_default_checkpoint_database():
    """Keep the module-level default graph database outside the repository."""
    yield

    from agent_os.graph import graph

    graph.checkpointer.conn.close()
    if _original_checkpoint_path is None:
        os.environ.pop(CHECKPOINT_DB_ENV, None)
    else:
        os.environ[CHECKPOINT_DB_ENV] = _original_checkpoint_path

    for key, val in (
        ("LLM_ARCHITECT", _original_architect),
        ("LLM_EXECUTOR", _original_executor),
    ):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val

    _checkpoint_directory.cleanup()
