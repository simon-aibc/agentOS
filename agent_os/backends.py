import inspect
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from agent_os.cli_backends import (
    build_safe_subprocess_env,
    parse_claude_stream_json,
    parse_codex_output_file,
    run_cli_command,
    write_schema_file,
)
from agent_os.plugins import PluginError, PluginRegistry
from agent_os.schemas import (
    CodingPlan,
    CodingResult,
    ExecutionResult,
    PlanArtifact,
)
from agent_os.state import AgentState

BackendRole = Literal["architect", "executor"]
BACKEND_ROLES = frozenset({"architect", "executor"})
BackendArtifact = PlanArtifact | ExecutionResult
BackendInvoker = Callable[[AgentState], BackendArtifact]


class _CodexCodingPlanOutput(BaseModel):
    """Codex-compatible projection without free-form mapping fields."""

    summary: str = ""
    steps: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    files: list[str]
    changes: list[str]
    verify_cmd: str


class _CodexCodingResultOutput(BaseModel):
    """Codex-compatible execution report without free-form mapping fields."""

    status: Literal["completed", "failed", "cancelled", "waiting"]
    artifacts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    diff: str = ""
    verify_output: str = ""


class AuthStatus(BaseModel):
    """Safe, structured authentication status for an adapter."""

    status: Literal["ok", "unauthenticated", "unknown"]
    detail: str = ""


class BackendAdapter(Protocol):
    """Contract implemented by providers that can serve graph roles."""

    name: str
    binary_name: str
    supported_roles: frozenset[BackendRole]
    stub: bool = False

    def build_invoker(self, role: BackendRole) -> BackendInvoker: ...

    def authentication_status(self) -> AuthStatus: ...


class BackendRegistry:
    """Resolve registered adapters by normalized backend name and role."""

    def __init__(self) -> None:
        self._adapters: dict[str, BackendAdapter] = {}

    @property
    def names(self) -> tuple[str, ...]:
        """Return registered names in stable order for diagnostics."""

        return tuple(sorted(self._adapters))

    def items(self) -> tuple[tuple[str, BackendAdapter], ...]:
        """Return registered names and adapters in stable order."""
        return tuple((name, self._adapters[name]) for name in self.names)

    def register(self, adapter: BackendAdapter) -> None:
        """Register an adapter, rejecting backend-name collisions."""

        name = adapter.name.strip()
        if not name:
            raise ValueError("Backend adapter name must not be empty.")
        if name in self._adapters:
            raise ValueError(f"Backend adapter name already registered: {name}")
        self._adapters[name] = adapter

    def resolve(self, role: BackendRole, name: str) -> BackendAdapter:
        """Resolve an adapter and verify that it supports the requested role."""

        adapter = self._adapters.get(name)
        if adapter is None:
            registered = ", ".join(self.names) or "(none)"
            raise ValueError(
                f"Unknown backend adapter '{name}'. Registered adapters: {registered}"
            )
        if role not in adapter.supported_roles:
            supported = ", ".join(sorted(adapter.supported_roles)) or "none"
            raise ValueError(
                f"Backend adapter '{name}' does not support role '{role}'. "
                f"Supported roles: {supported}"
            )
        return adapter


class NotYetSupportedError(RuntimeError):
    """Raised when a discoverable candidate adapter cannot be invoked yet."""


ANTIGRAVITY_NOT_SUPPORTED_MESSAGE = (
    "Antigravity backend is not yet supported. Two acceptance criteria must "
    "be met before this adapter ships: (1) documented noninteractive "
    "invocation form with parseable output; (2) enforceable permission mode "
    "equivalent to Claude plan/acceptEdits or Codex read-only/workspace-write. "
    "See docs/v1.2-portability.md v1.2 or this adapter's docstring for updates."
)


def _check_cli_auth_status(
    binary: str,
    args: list[str],
    success_indicators: list[str],
    unauth_indicators: list[str],
    unauth_detail: str,
) -> AuthStatus:
    import shutil
    import subprocess

    from agent_os.cli_backends import _coerce_text, _redact_secrets_in_text

    binary_path = shutil.which(binary)
    if not binary_path:
        return AuthStatus(
            status="unknown", detail=f"Binary '{binary}' not found on PATH."
        )

    try:
        result = subprocess.run(
            [binary_path] + args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            shell=False,
            env=build_safe_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return AuthStatus(
            status="unknown", detail=f"Command '{binary}' timed out during auth check."
        )
    except Exception as e:
        return AuthStatus(
            status="unknown", detail=f"Subprocess error: {type(e).__name__}"
        )

    output = _coerce_text(result.stdout) + "\n" + _coerce_text(result.stderr)
    lower_output = output.lower()

    if any(ind in lower_output for ind in unauth_indicators):
        return AuthStatus(status="unauthenticated", detail=unauth_detail)

    if result.returncode == 0 and any(
        ind in lower_output for ind in success_indicators
    ):
        return AuthStatus(status="ok", detail="Authenticated")

    excerpt = _redact_secrets_in_text(output[:200].replace("\n", " ").strip())
    return AuthStatus(
        status="unknown",
        detail=f"Exit {result.returncode}, unrecognized output. Excerpt: {excerpt}",
    )


class ClaudeCodeAdapter:
    """Claude Code adapter with role-specific permission modes."""

    name = "claude-code"
    binary_name = "claude"
    supported_roles = frozenset({"architect", "executor"})
    stub = False

    def build_invoker(self, role: BackendRole) -> BackendInvoker:
        if role not in self.supported_roles:
            raise ValueError(
                f"Backend adapter '{self.name}' does not support role '{role}'"
            )

        def invoker(state: AgentState) -> BackendArtifact:
            if role == "architect":
                from agent_os.agents.cli_architect import _build_architect_prompt

                model = CodingPlan
                prompt = _build_architect_prompt(state)
                permission_mode = "plan"
            else:
                from agent_os.agents.cli_executor import build_executor_prompt

                plan = state.get("plan")
                if not isinstance(plan, PlanArtifact):
                    raise ValueError("State 'plan' must be a PlanArtifact instance.")
                model = CodingResult
                prompt = build_executor_prompt(plan)
                permission_mode = "acceptEdits"

            args = [
                "-p",
                prompt,
                "--permission-mode",
                permission_mode,
                "--output-format",
                "stream-json",
                "--verbose",
                "--json-schema",
                json.dumps(model.model_json_schema()),
            ]
            result = run_cli_command(self.binary_name, args)
            parsed = parse_claude_stream_json(result.stdout)
            try:
                return model.model_validate(parsed)
            except ValidationError as exc:
                raise ValueError(
                    f"Failed to validate {self.name} structured output as "
                    f"{model.__name__}. The payload did not match the required schema."
                ) from exc

        return invoker

    def authentication_status(self) -> AuthStatus:
        return _check_cli_auth_status(
            self.binary_name,
            ["auth", "status"],
            success_indicators=[
                "logged in",
                '"loggedin": true',
                '"loggedin":true',
            ],
            unauth_indicators=[
                "not logged in",
                "no active session",
                "authentication_failed",
                '"loggedin": false',
                '"loggedin":false',
                '"authmethod": "none"',
            ],
            unauth_detail="run: claude auth login",
        )


class CodexAdapter:
    """Codex adapter with strict schemas and role-specific sandbox modes."""

    name = "codex"
    binary_name = "codex"
    supported_roles = frozenset({"architect", "executor"})
    stub = False

    def build_invoker(self, role: BackendRole) -> BackendInvoker:
        if role not in self.supported_roles:
            raise ValueError(
                f"Backend adapter '{self.name}' does not support role '{role}'"
            )

        def invoker(state: AgentState) -> BackendArtifact:
            if role == "architect":
                from agent_os.agents.cli_architect import _build_architect_prompt

                model = CodingPlan
                schema_model = _CodexCodingPlanOutput
                prompt = _build_architect_prompt(state)
                sandbox_mode = "read-only"
            else:
                from agent_os.agents.cli_executor import build_executor_prompt

                plan = state.get("plan")
                if not isinstance(plan, PlanArtifact):
                    raise ValueError("State 'plan' must be a PlanArtifact instance.")
                model = CodingResult
                schema_model = _CodexCodingResultOutput
                prompt = build_executor_prompt(plan)
                sandbox_mode = "workspace-write"

            with tempfile.TemporaryDirectory(prefix="agent-os-codex-") as temp_dir:
                output_path = Path(temp_dir) / "last-message.json"
                with write_schema_file(schema_model, strict=True) as schema_path:
                    args = [
                        "exec",
                        "--sandbox",
                        sandbox_mode,
                        "--output-schema",
                        str(schema_path),
                        "--output-last-message",
                        str(output_path),
                        prompt,
                    ]
                    run_cli_command(self.binary_name, args)
                parsed = parse_codex_output_file(output_path)

            try:
                compatible_output = schema_model.model_validate(parsed)
                return model.model_validate(compatible_output.model_dump())
            except ValidationError as exc:
                raise ValueError(
                    f"Failed to validate {self.name} structured output as "
                    f"{model.__name__}. The payload did not match the required schema."
                ) from exc

        return invoker

    def authentication_status(self) -> AuthStatus:
        return _check_cli_auth_status(
            self.binary_name,
            ["login", "status"],
            success_indicators=["logged in", "authenticated"],
            unauth_indicators=[
                "not logged in",
                "oauth session expired",
                "invalid_api_key",
                "unauthorized",
            ],
            unauth_detail="run: codex login",
        )


class AntigravityAdapter:
    """Candidate adapter gated on a stable CLI and enforceable permissions."""

    name = "antigravity"
    binary_name = "antigravity"
    supported_roles = frozenset({"architect", "executor"})
    stub = True

    def build_invoker(self, role: BackendRole) -> BackendInvoker:
        if role not in self.supported_roles:
            raise ValueError(
                f"Backend adapter '{self.name}' does not support role '{role}'"
            )

        def invoker(state: AgentState) -> BackendArtifact:
            del state
            raise NotYetSupportedError(ANTIGRAVITY_NOT_SUPPORTED_MESSAGE)

        return invoker

    def authentication_status(self) -> AuthStatus:
        return AuthStatus(
            status="unknown",
            detail=(
                "Antigravity adapter is a candidate stub; auth check not "
                "implemented until acceptance criteria are met."
            ),
        )


BUILT_IN_BACKEND_NAMES: frozenset[str] = frozenset(
    {"claude-code", "codex", "antigravity"}
)


def build_default_registry(
    plugin_registry: PluginRegistry | None = None,
) -> BackendRegistry:
    """Build the backend registry with built-in adapters and discovered plugins."""
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    # 1. Unconditionally discover plugins to protect built-in names
    try:
        discovered_backends = plugin_registry.discover("agent_os.backends")
    except PluginError as exc:
        raise ValueError(
            f"Plugin discovery failed for group 'agent_os.backends': {exc}"
        ) from exc

    for ep_name in discovered_backends:
        if ep_name in BUILT_IN_BACKEND_NAMES:
            raise ValueError(
                f"Plugin in 'agent_os.backends' cannot override built-in backend '{ep_name}'."
            )

    registry = BackendRegistry()
    registry.register(ClaudeCodeAdapter())
    registry.register(CodexAdapter())
    registry.register(AntigravityAdapter())

    # 2. Instantiate and register discovered backend adapter plugins
    for ep_name in discovered_backends:
        try:
            plugin_obj = plugin_registry.resolve("agent_os.backends", ep_name)
        except PluginError as exc:
            raise ValueError(
                f"Failed to load backend plugin '{ep_name}': {exc}"
            ) from exc

        if isinstance(plugin_obj, type) or (
            callable(plugin_obj) and not hasattr(plugin_obj, "name")
        ):
            sig = inspect.signature(plugin_obj)
            required_params = [
                p.name
                for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            ]
            if required_params:
                raise ValueError(
                    f"Backend plugin '{ep_name}' in group 'agent_os.backends' requires unsupported arguments: "
                    f"{required_params}. Backend factory must accept 0 required arguments."
                )
            try:
                adapter_inst = plugin_obj()
            except Exception as exc:
                raise ValueError(
                    f"Failed to instantiate backend plugin '{ep_name}': {exc}"
                ) from exc
        else:
            adapter_inst = plugin_obj

        if not (
            hasattr(adapter_inst, "name")
            and isinstance(adapter_inst.name, str)
            and hasattr(adapter_inst, "binary_name")
            and hasattr(adapter_inst, "supported_roles")
            and callable(getattr(adapter_inst, "build_invoker", None))
            and callable(getattr(adapter_inst, "authentication_status", None))
        ):
            raise ValueError(
                f"Backend plugin '{ep_name}' does not satisfy the BackendAdapter interface."
            )

        registry.register(adapter_inst)

    return registry


get_default_backend_registry = build_default_registry
