from dataclasses import FrozenInstanceError

import pytest

from agent_os.principal import (
    LocalPrincipalResolver,
    Principal,
    PrincipalResolver,
)


def test_principal_immutability():
    p = Principal(id="local:alice", kind="human", display="Alice")
    assert p.id == "local:alice"
    assert p.kind == "human"
    assert p.display == "Alice"
    assert p.on_behalf_of is None

    with pytest.raises(FrozenInstanceError):
        p.id = "local:bob"  # type: ignore[misc]


def test_principal_validation():
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        Principal(id="", kind="human", display="Alice")

    with pytest.raises(ValueError, match="kind must be a non-empty string"):
        Principal(id="local:alice", kind="", display="Alice")

    with pytest.raises(ValueError, match="display must be a non-empty string"):
        Principal(id="local:alice", kind="human", display="")


def test_local_principal_resolver_cli():
    resolver = LocalPrincipalResolver()
    assert isinstance(resolver, PrincipalResolver)

    p = resolver.resolve()
    assert p.kind == "human"
    assert p.id.startswith("local:")
    assert "Local User" in p.display


def test_local_principal_resolver_schedule():
    resolver = LocalPrincipalResolver()

    # From dict
    p1 = resolver.resolve({"schedule_id": "nightly_sync"})
    assert p1.id == "schedule:nightly_sync"
    assert p1.kind == "schedule"
    assert p1.display == "Schedule (nightly_sync)"

    # From string
    p2 = resolver.resolve("schedule:morning_brief")
    assert p2.id == "schedule:morning_brief"
    assert p2.kind == "schedule"

    # From object with attribute
    class DummySched:
        schedule_id = "cron_cleanup"

    p3 = resolver.resolve(DummySched())
    assert p3.id == "schedule:cron_cleanup"
    assert p3.kind == "schedule"


def test_local_principal_resolver_never_accesses_raw_credential_headers():
    resolver = LocalPrincipalResolver()

    class ExplodingHeaders:
        def __getitem__(self, item):
            raise AssertionError("Resolver must not read request headers!")

        def get(self, *args, **kwargs):
            raise AssertionError("Resolver must not read request headers!")

    class FakeState:
        authenticated_with_token = True
        is_loopback = False

    class FakeClient:
        host = "192.168.1.100"

    class FakeRequest:
        def __init__(self):
            self.headers = ExplodingHeaders()
            self.state = FakeState()
            self.client = FakeClient()

    req = FakeRequest()
    p = resolver.resolve(req)
    assert p.id == "token:execution"
    assert p.kind == "service"
    assert p.on_behalf_of is None


def test_local_principal_resolver_http_middleware_state():
    resolver = LocalPrincipalResolver()

    class FakeClient:
        def __init__(self, host):
            self.host = host

    class FakeState:
        def __init__(self, authenticated_with_token=False, is_loopback=False):
            self.authenticated_with_token = authenticated_with_token
            self.is_loopback = is_loopback

    class FakeRequest:
        def __init__(self, state, client):
            self.state = state
            self.client = client

    # 1. Authenticated via token on loopback
    req1 = FakeRequest(FakeState(authenticated_with_token=True, is_loopback=True), FakeClient("127.0.0.1"))
    p1 = resolver.resolve(req1)
    assert p1.id == "token:execution"
    assert p1.kind == "service"
    assert p1.on_behalf_of is None

    # 2. Authenticated via token on non-loopback
    req2 = FakeRequest(FakeState(authenticated_with_token=True, is_loopback=False), FakeClient("10.0.0.5"))
    p2 = resolver.resolve(req2)
    assert p2.id == "token:execution"
    assert p2.kind == "service"
    assert p2.on_behalf_of is None

    # 3. Unauthenticated on loopback (local user)
    req3 = FakeRequest(FakeState(authenticated_with_token=False, is_loopback=True), FakeClient("127.0.0.1"))
    p3 = resolver.resolve(req3)
    assert p3.kind == "human"
    assert p3.id.startswith("local:")
    assert p3.on_behalf_of is None
