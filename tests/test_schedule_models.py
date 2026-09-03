"""Tests for ScheduleInput validation and payload serialization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_os.schedule_models import ScheduleInput


def test_app_callback_valid_defaults():
    inp = ScheduleInput(
        name="webhook-1",
        kind="app_callback",
        every="1h",
        url="https://example.com/webhook",
    )
    assert inp.name == "webhook-1"
    assert inp.kind == "app_callback"
    assert inp.url == "https://example.com/webhook"
    assert inp.method == "POST"
    assert inp.trigger_kind == "interval"
    assert inp.trigger_value == "1h"
    assert inp.payload == {
        "url": "https://example.com/webhook",
        "method": "POST",
    }


def test_app_callback_method_normalization():
    for verb in ["get", "post", "put", "patch", "delete"]:
        inp = ScheduleInput(
            name="hook",
            kind="app_callback",
            every="30s",
            url="http://localhost:8000/callback",
            method=verb,
        )
        assert inp.method == verb.upper()
        assert inp.payload["method"] == verb.upper()


def test_app_callback_with_headers_and_body():
    inp = ScheduleInput(
        name="full-hook",
        kind="app_callback",
        cron="0 * * * *",
        url="https://example.com/events",
        method="POST",
        headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
        body={"action": "sync", "count": 42},
    )
    assert inp.payload == {
        "url": "https://example.com/events",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
        },
        "body": {"action": "sync", "count": 42},
    }


def test_app_callback_requires_url():
    with pytest.raises(ValidationError, match="kind=app_callback requires 'url'"):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
        )


def test_app_callback_invalid_url_scheme():
    with pytest.raises(
        ValidationError, match="url must start with 'http://' or 'https://'"
    ):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="ftp://example.com/hook",
        )

    with pytest.raises(
        ValidationError, match="url must start with 'http://' or 'https://'"
    ):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="example.com/hook",
        )

    with pytest.raises(ValidationError, match="include a valid host"):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="https://",
        )


def test_app_callback_invalid_method():
    with pytest.raises(ValidationError, match="method must be one of"):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="https://example.com/hook",
            method="CONNECT",
        )


def test_app_callback_forbids_task():
    with pytest.raises(
        ValidationError, match="kind=app_callback does not accept 'task'"
    ):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="https://example.com/hook",
            task="should fail",
        )


def test_app_callback_forbids_workspace():
    with pytest.raises(
        ValidationError, match="kind=app_callback does not accept 'workspace'"
    ):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="https://example.com/hook",
            workspace="/tmp/ws",
        )

    with pytest.raises(ValidationError, match="does not accept 'workspace'"):
        ScheduleInput(
            name="bad",
            kind="app_callback",
            every="1h",
            url="https://example.com/hook",
            workspace="",
        )


def test_run_forbids_callback_fields():
    with pytest.raises(ValidationError, match="kind=run does not accept"):
        ScheduleInput(
            name="bad-run",
            kind="run",
            every="1h",
            task="do work",
            url="https://example.com/hook",
        )

    with pytest.raises(ValidationError, match="kind=run does not accept"):
        ScheduleInput(
            name="bad-run",
            kind="run",
            every="1h",
            task="do work",
            headers={"X-Key": "val"},
        )


def test_brief_forbids_callback_fields():
    with pytest.raises(ValidationError, match="kind=brief does not accept"):
        ScheduleInput(
            name="bad-brief",
            kind="brief",
            every="1h",
            url="https://example.com/hook",
        )

    with pytest.raises(ValidationError, match="kind=brief does not accept"):
        ScheduleInput(
            name="bad-brief",
            kind="brief",
            every="1h",
            body={"data": 123},
        )
