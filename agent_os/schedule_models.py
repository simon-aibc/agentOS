"""FastAPI-independent domain input model for schedule creation.

Used by both the CLI and API so validation rules cannot drift.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_os.schedules import ALLOWED_HTTP_METHODS, validate_app_callback_url


class ScheduleInput(BaseModel):
    """Shared, validated input for creating a schedule.

    Enforces the mutual-exclusion and kind-specific payload rules
    specified in the design contract.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    kind: Literal["run", "brief", "app_callback"]
    cron: str | None = None
    every: str | None = None
    timezone: str = "UTC"
    task: str | None = Field(default=None, min_length=1, max_length=4096)
    workspace: str | None = None
    url: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None

    @field_validator("name", "task", "url", mode="after")
    @classmethod
    def _strip_and_check_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("String cannot be empty or only whitespace")
        return v

    @model_validator(mode="after")
    def _cross_field_rules(self) -> ScheduleInput:
        # Exactly one of cron/every.
        if self.cron and self.every:
            raise ValueError("Specify exactly one of 'cron' and 'every', not both")
        if not self.cron and not self.every:
            raise ValueError("Specify exactly one of 'cron' and 'every'")

        # Kind-specific payload validation.
        if self.kind == "run":
            if not self.task:
                raise ValueError("kind=run requires 'task'")
            if (
                self.url is not None
                or self.method is not None
                or self.headers is not None
                or self.body is not None
            ):
                raise ValueError(
                    "kind=run does not accept 'url', 'method', 'headers', or 'body'"
                )
        elif self.kind == "brief":
            if self.task is not None:
                raise ValueError("kind=brief does not accept 'task'")
            if self.workspace is not None:
                raise ValueError("kind=brief does not accept 'workspace'")
            if (
                self.url is not None
                or self.method is not None
                or self.headers is not None
                or self.body is not None
            ):
                raise ValueError(
                    "kind=brief does not accept 'url', 'method', 'headers', or 'body'"
                )
        elif self.kind == "app_callback":
            if self.task is not None:
                raise ValueError("kind=app_callback does not accept 'task'")
            if self.workspace is not None:
                raise ValueError("kind=app_callback does not accept 'workspace'")
            if not self.url:
                raise ValueError("kind=app_callback requires 'url'")
            self.url = validate_app_callback_url(self.url)
            method = (self.method or "POST").upper()
            if method not in ALLOWED_HTTP_METHODS:
                raise ValueError(
                    f"method must be one of {sorted(ALLOWED_HTTP_METHODS)}, "
                    f"got {self.method!r}"
                )
            self.method = method

        return self

    @property
    def trigger_kind(self) -> Literal["cron", "interval"]:
        return "cron" if self.cron else "interval"

    @property
    def trigger_value(self) -> str:
        return self.cron or self.every or ""

    @property
    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.kind == "run":
            if self.task:
                result["task"] = self.task
            if self.workspace:
                result["workspace"] = self.workspace
        elif self.kind == "app_callback":
            if self.url:
                result["url"] = self.url
            if self.method:
                result["method"] = self.method
            if self.headers is not None:
                result["headers"] = self.headers
            if self.body is not None:
                result["body"] = self.body
        return result
