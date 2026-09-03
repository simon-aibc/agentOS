import inspect
import logging
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from agent_os.backends import BackendRegistry, build_default_registry
from agent_os.bindings import resolve_backend_binding
from agent_os.connectors import (
    ConnectorRegistry,
    FilesystemConnector,
    GbrainConnector,
    MarkdownVaultConnector,
    MemoryConnector,
    WritableMemory,
)
from agent_os.context import ContextProvider
from agent_os.default_registry import build_default_registry as build_skill_registry
from agent_os.events import EventSink
from agent_os.hot_context import load_hot_context
from agent_os.permission_store import (
    DEFAULT_PERMISSION_DB,
    PERMISSION_DB_ENV,
    SqlitePermissionStore,
)
from agent_os.plugins import PluginError, PluginRegistry
from agent_os.policy import CompositePolicyEngine, LocalPolicy, PolicyEngine
from agent_os.skill_packages import SkillPackageLoader
from agent_os.skills import SkillRegistry
from agent_os.state import BackendBinding
from agent_os.webhooks import WebhookEventSink, is_safe_webhook_url

logger = logging.getLogger(__name__)


class WorkspaceLoadError(ValueError):
    """Raised for invalid, unsafe, or unresolvable workspace configuration."""


class WorkspaceMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    department: str | None = None
    organization: str | None = None


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceMeta
    backends: dict[str, str | None] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    memory: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    webhooks: dict[str, Any] = Field(default_factory=dict)

    _base_path: Path = PrivateAttr(default_factory=lambda: Path.cwd().resolve())

    @property
    def base_path(self) -> Path:
        return self._base_path


@dataclass(frozen=True)
class ComposedWorkspace:
    workspace: Workspace
    backend_binding: BackendBinding
    backend_registry: BackendRegistry
    skill_registry: SkillRegistry
    connector_registry: ConnectorRegistry
    memory_connector: MemoryConnector
    policy: PolicyEngine
    hot_context: str
    limits: dict[str, Any]
    environment: dict[str, str | None]
    context_providers: list[ContextProvider] = field(default_factory=list)
    event_sinks: list[EventSink] = field(default_factory=list)


_BACKEND_ROLES = frozenset({"router", "architect", "executor"})
_PATH_KEYS = frozenset({"path", "root", "root_path", "vault_path"})
_RESTRICTED_FILENAMES = frozenset({"db_config.php", "mail_config.php", ".env"})


def resolve_permission_db_path(workspace: Workspace | None = None) -> str:
    """Resolve the permissions DB without sharing rules across workspaces.

    An explicitly configured environment path remains an operator override.
    Otherwise, a workspace owns a ``permissions.db`` beside its TOML file;
    standalone local use keeps the historical default path.
    """
    configured = os.getenv(PERMISSION_DB_ENV, "").strip()
    if configured:
        return str(Path(configured).expanduser().resolve())
    if workspace is not None:
        return str((workspace.base_path / "permissions.db").resolve())
    return DEFAULT_PERMISSION_DB


def open_permission_store(
    workspace: Workspace | None = None,
) -> SqlitePermissionStore | None:
    """Open the workspace (or standalone) rule store with a fail-safe fallback."""
    db_path = resolve_permission_db_path(workspace)
    try:
        return SqlitePermissionStore(db_path)
    except Exception as exc:
        logger.warning(
            f"Failed to initialize SqlitePermissionStore at '{db_path}': {exc}"
        )
        return None


def load_workspace(path: str | Path) -> Workspace:
    """Load and validate a workspace TOML file."""

    workspace_path = _resolve_workspace_path(path)
    _reject_restricted_path(workspace_path)

    try:
        with workspace_path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceLoadError(
            f"Malformed TOML in workspace file '{workspace_path}': {exc}"
        ) from exc
    except OSError as exc:
        raise WorkspaceLoadError(
            f"Unable to read workspace file '{workspace_path}': {exc}"
        ) from exc

    try:
        workspace = Workspace.model_validate(data)
    except Exception as exc:
        raise WorkspaceLoadError(
            f"Invalid workspace configuration in '{workspace_path}': {exc}"
        ) from exc

    workspace._base_path = workspace_path.parent
    workspace = _resolve_workspace_paths(workspace)
    plugin_registry = PluginRegistry()
    backend_registry = build_default_registry(plugin_registry=plugin_registry)
    _validate_backend_bindings(workspace, backend_registry)
    connector_registry, memory_connector = _build_connector_registry(
        workspace, plugin_registry=plugin_registry
    )
    _build_workspace_skill_registry(
        workspace, memory_connector, plugin_registry=plugin_registry
    )
    _validate_connector_references(workspace, connector_registry)
    _build_policy(workspace, store=None, plugin_registry=plugin_registry)
    _build_context_providers(workspace, plugin_registry=plugin_registry)
    _build_event_sinks(workspace, plugin_registry=plugin_registry)
    return workspace


def compose_workspace(ws: Workspace) -> ComposedWorkspace:
    """Compose a workspace into isolated runtime registries and bindings."""
    plugin_registry = PluginRegistry()

    backend_registry = build_default_registry(plugin_registry=plugin_registry)
    _validate_backend_bindings(ws, backend_registry)
    connector_registry, memory_connector = _build_connector_registry(
        ws, plugin_registry=plugin_registry
    )
    skill_registry = _build_workspace_skill_registry(
        ws, memory_connector, plugin_registry=plugin_registry
    )
    _validate_connector_references(ws, connector_registry)

    environment = _workspace_environment(ws)
    with _patched_environment(environment):
        backend_binding = resolve_backend_binding(f"workspace:{ws.workspace.name}")

    context_config = ws.context
    sources = _optional_string_list(context_config.get("sources"), "context.sources")
    hot_context = load_hot_context(
        memory_connector,
        max_chars=int(context_config.get("max_chars", 8000)),
        max_age_days=int(context_config.get("max_age_days", 14)),
        sources=sources,
    )

    store = open_permission_store(ws)
    policy = _build_policy(ws, store=store, plugin_registry=plugin_registry)
    context_providers = _build_context_providers(ws, plugin_registry=plugin_registry)
    event_sinks = _build_event_sinks(ws, plugin_registry=plugin_registry)

    return ComposedWorkspace(
        workspace=ws,
        backend_binding=backend_binding,
        backend_registry=backend_registry,
        skill_registry=skill_registry,
        connector_registry=connector_registry,
        memory_connector=memory_connector,
        policy=policy,
        hot_context=hot_context,
        limits=dict(ws.limits),
        environment=environment,
        context_providers=context_providers,
        event_sinks=event_sinks,
    )


def _resolve_workspace_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        candidate = candidate / "workspace.toml"
    return candidate.resolve()


def _reject_restricted_path(path: Path) -> None:
    name = path.name.lower()
    if (
        name in _RESTRICTED_FILENAMES
        or "credentials" in name
        or "token" in name
        or name.startswith("auth")
    ):
        raise WorkspaceLoadError(
            f"Refusing to load restricted configuration path: {path}"
        )


def _resolve_workspace_paths(ws: Workspace) -> Workspace:
    memory = dict(ws.memory)
    for key in _PATH_KEYS:
        value = memory.get(key)
        if isinstance(value, str) and value.strip():
            memory[key] = _resolve_relative_path(ws.base_path, value)

    resolved_skills = []
    for skill in ws.skills:
        candidate = Path(skill).expanduser()
        if not candidate.is_absolute():
            candidate = ws.base_path / candidate
        resolved_skills.append(
            str(candidate.resolve()) if candidate.exists() else skill
        )

    return ws.model_copy(update={"memory": memory, "skills": resolved_skills})


def _resolve_relative_path(base_path: Path, value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_path / path
    return str(path.resolve())


def _validate_backend_bindings(
    ws: Workspace,
    registry: BackendRegistry,
) -> None:
    for role, value in ws.backends.items():
        if role not in _BACKEND_ROLES:
            expected = ", ".join(sorted(_BACKEND_ROLES))
            raise WorkspaceLoadError(
                f"Unknown backend role '{role}' in workspace '{ws.workspace.name}'. "
                f"Expected one of: {expected}."
            )
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceLoadError(
                f"Backend value for role '{role}' must be a non-empty string."
            )
        backend_value = value.strip()
        if role == "router":
            if backend_value.startswith("cli/"):
                raise WorkspaceLoadError(
                    f"Router cannot be a CLI backend: {backend_value}"
                )
            continue
        backend_name = (
            backend_value[4:] if backend_value.startswith("cli/") else backend_value
        )
        try:
            registry.resolve(role, backend_name)
        except ValueError as exc:
            raise WorkspaceLoadError(
                f"Workspace backend '{role}' is not resolvable: {exc}"
            ) from exc


def _build_workspace_skill_registry(
    ws: Workspace,
    memory_connector: WritableMemory | None = None,
    plugin_registry: PluginRegistry | None = None,
) -> SkillRegistry:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    if str(ws.policy.get("mode", "")).strip().lower() == "public-read-only":
        registry = SkillRegistry()
    else:
        registry = build_skill_registry(memory_connector=memory_connector)
    loader = SkillPackageLoader(registry)

    # 1. Discover and load plugins from 'agent_os.skill_packages'
    try:
        loader.load_from_entry_points(plugin_registry)
    except Exception as exc:
        raise WorkspaceLoadError(
            f"Failed to load skill package plugins: {exc}"
        ) from exc

    # 2. Process workspace-declared skills
    for entry in ws.skills:
        if registry.get(entry) is not None:
            continue

        path = Path(entry).expanduser()
        if path.exists():
            before = set(registry.names())
            try:
                if path.is_dir() and (path / "manifest.toml").exists():
                    loader.load_package(path)
                elif path.is_dir():
                    loader.load_from_directories([str(path)])
                else:
                    raise WorkspaceLoadError(
                        f"Skill path '{entry}' is not a directory."
                    )
            except Exception as exc:
                if isinstance(exc, WorkspaceLoadError):
                    raise
                raise WorkspaceLoadError(
                    f"Workspace skill '{entry}' is not resolvable: {exc}"
                ) from exc
            if set(registry.names()) == before:
                raise WorkspaceLoadError(
                    f"Workspace skill path '{entry}' did not register any skills."
                )
            continue

        if _looks_like_path(entry):
            raise WorkspaceLoadError(f"Workspace skill path '{entry}' does not exist.")

        available = ", ".join(registry.names()) or "none"
        raise WorkspaceLoadError(
            f"Unknown workspace skill '{entry}'. Available skills: {available}."
        )

    return registry


BUILT_IN_POLICY_NAMES: frozenset[str] = frozenset({"local", "manual", "smart", "off"})


def _build_policy(
    ws: Workspace,
    store: Any = None,
    plugin_registry: PluginRegistry | None = None,
) -> PolicyEngine:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    policy_rules = dict(ws.policy)
    policy_mode = str(policy_rules.get("mode", "manual"))
    base_policy = LocalPolicy(policy_rules, store=store, mode=policy_mode)

    # Discover 'agent_os.policies' entry points to protect built-in names
    try:
        discovered_policy_plugins = plugin_registry.discover("agent_os.policies")
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Plugin discovery failed for group 'agent_os.policies': {exc}"
        ) from exc

    for ep_name in discovered_policy_plugins:
        if ep_name in BUILT_IN_POLICY_NAMES:
            raise WorkspaceLoadError(
                f"Plugin in 'agent_os.policies' cannot override built-in policy '{ep_name}'."
            )

    configured_plugins = policy_rules.get("plugins", policy_rules.get("policies", []))
    if isinstance(configured_plugins, str):
        configured_plugins = [configured_plugins]

    if not configured_plugins:
        return base_policy

    loaded_plugins: list[PolicyEngine] = []
    for plugin_name in configured_plugins:
        clean_name = str(plugin_name).strip()
        if clean_name not in discovered_policy_plugins:
            available = (
                ", ".join(plugin_registry.list_available("agent_os.policies")) or "none"
            )
            raise WorkspaceLoadError(
                f"Unknown policy plugin '{clean_name}'. Available policy plugins: {available}."
            )

        try:
            plugin_obj = plugin_registry.resolve("agent_os.policies", clean_name)
        except PluginError as exc:
            raise WorkspaceLoadError(
                f"Failed to load policy plugin '{clean_name}': {exc}"
            ) from exc

        if isinstance(plugin_obj, type) or (
            callable(plugin_obj) and not hasattr(plugin_obj, "evaluate")
        ):
            sig = inspect.signature(plugin_obj)
            if not sig.parameters:
                plugin_inst = plugin_obj()
            else:
                kwargs = {k: v for k, v in policy_rules.items() if k in sig.parameters}
                plugin_inst = plugin_obj(**kwargs)
        else:
            plugin_inst = plugin_obj

        if not (
            hasattr(plugin_inst, "evaluate")
            and callable(getattr(plugin_inst, "evaluate", None))
        ):
            raise WorkspaceLoadError(
                f"Policy plugin '{clean_name}' does not implement evaluate() method."
            )

        loaded_plugins.append(plugin_inst)

    return CompositePolicyEngine(base_policy, loaded_plugins)


BUILT_IN_CONTEXT_PROVIDER_NAMES: frozenset[str] = frozenset(
    {"hot_context", "memory", "vault", "static"}
)


def _build_context_providers(
    ws: Workspace,
    plugin_registry: PluginRegistry | None = None,
) -> list[ContextProvider]:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    # Discover entry points to protect built-in names
    try:
        discovered_providers = plugin_registry.discover("agent_os.context_providers")
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Plugin discovery failed for group 'agent_os.context_providers': {exc}"
        ) from exc

    for ep_name in discovered_providers:
        if ep_name in BUILT_IN_CONTEXT_PROVIDER_NAMES:
            raise WorkspaceLoadError(
                f"Plugin in 'agent_os.context_providers' cannot override built-in context provider '{ep_name}'."
            )

    configured_providers = ws.context.get("providers", [])
    if isinstance(configured_providers, str):
        configured_providers = [configured_providers]
    elif not isinstance(configured_providers, list):
        configured_providers = []

    if not configured_providers:
        return []

    loaded: list[ContextProvider] = []
    for prov_name in configured_providers:
        clean_name = str(prov_name).strip()
        if clean_name not in discovered_providers:
            available = (
                ", ".join(plugin_registry.list_available("agent_os.context_providers"))
                or "none"
            )
            raise WorkspaceLoadError(
                f"Unknown context provider '{clean_name}'. Available context providers: {available}."
            )

        try:
            plugin_obj = plugin_registry.resolve(
                "agent_os.context_providers", clean_name
            )
        except PluginError as exc:
            raise WorkspaceLoadError(
                f"Failed to load context provider plugin '{clean_name}': {exc}"
            ) from exc

        if isinstance(plugin_obj, type) or (
            callable(plugin_obj) and not hasattr(plugin_obj, "provide")
        ):
            sig = inspect.signature(plugin_obj)
            if not sig.parameters:
                provider_inst = plugin_obj()
            else:
                kwargs = {k: v for k, v in ws.context.items() if k in sig.parameters}
                provider_inst = plugin_obj(**kwargs)
        else:
            provider_inst = plugin_obj

        if not (
            hasattr(provider_inst, "name")
            and hasattr(provider_inst, "provide")
            and callable(getattr(provider_inst, "provide", None))
        ):
            raise WorkspaceLoadError(
                f"Context provider plugin '{clean_name}' does not satisfy the ContextProvider interface."
            )

        loaded.append(provider_inst)

    return loaded


BUILT_IN_EVENT_SINK_NAMES: frozenset[str] = frozenset(
    {"webhook", "console", "null", "noop"}
)


def _build_event_sinks(
    ws: Workspace,
    plugin_registry: PluginRegistry | None = None,
) -> list[EventSink]:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    # Discover entry points to protect built-in names
    try:
        discovered_sinks = plugin_registry.discover("agent_os.event_sinks")
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Plugin discovery failed for group 'agent_os.event_sinks': {exc}"
        ) from exc

    for ep_name in discovered_sinks:
        if ep_name in BUILT_IN_EVENT_SINK_NAMES:
            raise WorkspaceLoadError(
                f"Plugin in 'agent_os.event_sinks' cannot override built-in event sink '{ep_name}'."
            )

    loaded: list[EventSink] = []

    # 1. Custom plugin sinks configured in [webhooks.sinks]
    configured_sinks = ws.webhooks.get("sinks", [])
    if isinstance(configured_sinks, str):
        configured_sinks = [configured_sinks]
    elif not isinstance(configured_sinks, list):
        configured_sinks = []

    for sink_name in configured_sinks:
        clean_name = str(sink_name).strip()
        if clean_name not in discovered_sinks:
            available = (
                ", ".join(plugin_registry.list_available("agent_os.event_sinks"))
                or "none"
            )
            raise WorkspaceLoadError(
                f"Unknown event sink '{clean_name}'. Available event sinks: {available}."
            )

        try:
            plugin_obj = plugin_registry.resolve("agent_os.event_sinks", clean_name)
        except PluginError as exc:
            raise WorkspaceLoadError(
                f"Failed to load event sink plugin '{clean_name}': {exc}"
            ) from exc

        if isinstance(plugin_obj, type) or (
            callable(plugin_obj) and not hasattr(plugin_obj, "emit")
        ):
            sig = inspect.signature(plugin_obj)
            if not sig.parameters:
                sink_inst = plugin_obj()
            else:
                kwargs = {k: v for k, v in ws.webhooks.items() if k in sig.parameters}
                sink_inst = plugin_obj(**kwargs)
        else:
            sink_inst = plugin_obj

        if not (
            hasattr(sink_inst, "name")
            and hasattr(sink_inst, "emit")
            and callable(getattr(sink_inst, "emit", None))
        ):
            raise WorkspaceLoadError(
                f"Event sink plugin '{clean_name}' does not satisfy the EventSink interface."
            )

        loaded.append(sink_inst)

    # 2. Built-in WebhookEventSink if [webhooks] has url
    if "secret" in ws.webhooks:
        raise WorkspaceLoadError(
            "Webhook secrets must be referenced by 'secret_env'; literal secrets in "
            "workspace configuration are not allowed."
        )
    url = str(ws.webhooks.get("url", "")).strip()
    if url:
        secret_env = str(ws.webhooks.get("secret_env", "")).strip()
        if not secret_env:
            logger.warning(
                "Webhook URL is configured in workspace, but 'secret_env' is not specified. "
                "Webhook delivery is safely disabled."
            )
        else:
            secret = os.getenv(secret_env, "").strip()
            if not secret:
                logger.warning(
                    f"Webhook 'secret_env' points to environment variable '{secret_env}', "
                    "which is unset or empty. Webhook delivery is safely disabled."
                )
            else:
                allowed = ws.webhooks.get("allowed_internal_hosts", [])
                if isinstance(allowed, str):
                    allowed = [allowed]
                elif not isinstance(allowed, list):
                    allowed = []
                allow_insecure_http = bool(
                    ws.webhooks.get("allow_insecure_http", False)
                )
                is_safe, reason = is_safe_webhook_url(
                    url,
                    allowed,
                    allow_insecure_http=allow_insecure_http,
                )
                if not is_safe:
                    raise WorkspaceLoadError(
                        f"Invalid webhook URL configuration: {reason}"
                    )
                max_retries = int(ws.webhooks.get("max_retries", 3))
                timeout_s = float(ws.webhooks.get("timeout_seconds", 2.0))
                loaded.append(
                    WebhookEventSink(
                        url=url,
                        secret=secret,
                        allowed_internal_hosts=allowed,
                        allow_insecure_http=allow_insecure_http,
                        max_retries=max_retries,
                        timeout_seconds=timeout_s,
                    )
                )

    return loaded


BUILT_IN_MEMORY_NAMES: frozenset[str] = frozenset(
    {"markdown", "markdown_vault", "gbrain"}
)
BUILT_IN_ACTION_NAMES: frozenset[str] = frozenset(
    {"filesystem", "memory", "markdown", "markdown_vault", "gbrain"}
)


def _build_connector_registry(
    ws: Workspace,
    plugin_registry: PluginRegistry | None = None,
) -> tuple[ConnectorRegistry, WritableMemory]:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    # 1. Unconditionally discover and check action connector plugins to protect built-in names
    try:
        discovered_action_plugins = plugin_registry.discover("agent_os.connectors")
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Plugin discovery failed for group 'agent_os.connectors': {exc}"
        ) from exc

    for ep_name in discovered_action_plugins:
        if ep_name in BUILT_IN_ACTION_NAMES:
            raise WorkspaceLoadError(
                f"Plugin in 'agent_os.connectors' cannot override built-in connector '{ep_name}'."
            )

    registry = ConnectorRegistry()
    registry.register("filesystem", FilesystemConnector())

    memory_connector = _build_memory_connector(ws, plugin_registry=plugin_registry)
    registry.register("memory", memory_connector)
    try:
        registry.register(memory_connector.name, memory_connector)
    except ValueError as exc:
        raise WorkspaceLoadError(
            f"Failed to register memory connector '{memory_connector.name}': {exc}"
        ) from exc
    if memory_connector.name == "markdown_vault":
        registry.register("markdown", memory_connector)

    # 2. Instantiate and register discovered action connector plugins
    for ep_name in discovered_action_plugins:
        try:
            plugin_obj = plugin_registry.resolve("agent_os.connectors", ep_name)
        except PluginError as exc:
            raise WorkspaceLoadError(
                f"Failed to load action connector plugin '{ep_name}': {exc}"
            ) from exc

        # Minimal supported entry-point contract:
        # - Instance: object with attributes (not a class/type)
        # - Class / Zero-argument factory: callable taking 0 required arguments
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
                raise WorkspaceLoadError(
                    f"Action connector plugin '{ep_name}' in group 'agent_os.connectors' requires unsupported arguments: "
                    f"{required_params}. Action connector factory must accept 0 required arguments."
                )
            try:
                conn_inst = plugin_obj()
            except Exception as exc:
                raise WorkspaceLoadError(
                    f"Failed to instantiate action connector plugin '{ep_name}': {exc}"
                ) from exc
        else:
            conn_inst = plugin_obj

        required_action_methods = ("capabilities", "describe_side_effect", "invoke")
        if not (
            hasattr(conn_inst, "name")
            and isinstance(conn_inst.name, str)
            and all(
                callable(getattr(conn_inst, method, None))
                for method in required_action_methods
            )
        ):
            raise WorkspaceLoadError(
                f"Action connector plugin '{ep_name}' does not satisfy the Connector interface "
                "(requires 'name' string plus callable 'capabilities', "
                "'describe_side_effect', and 'invoke')."
            )

        registry.register(ep_name, conn_inst)

    return registry, memory_connector


def _build_memory_connector(
    ws: Workspace,
    plugin_registry: PluginRegistry | None = None,
) -> WritableMemory:
    if plugin_registry is None:
        plugin_registry = PluginRegistry()

    # 1. Unconditionally discover and check memory connector plugins to protect built-in names
    try:
        discovered_memory_plugins = plugin_registry.discover(
            "agent_os.memory_connectors"
        )
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Plugin discovery failed for group 'agent_os.memory_connectors': {exc}"
        ) from exc

    for ep_name in discovered_memory_plugins:
        if ep_name in BUILT_IN_MEMORY_NAMES:
            raise WorkspaceLoadError(
                f"Plugin in 'agent_os.memory_connectors' cannot override built-in memory connector '{ep_name}'."
            )

    memory_type = (
        str(ws.memory.get("type", ws.memory.get("connector", "markdown")))
        .strip()
        .lower()
    )

    # 2. Built-in types
    if memory_type in ("markdown", "markdown_vault"):
        root = ws.memory.get(
            "path", ws.memory.get("root_path", ws.memory.get("root", "."))
        )
        root_path = _resolve_relative_path(ws.base_path, str(root))
        return MarkdownVaultConnector(root_path)
    if memory_type == "gbrain":
        return GbrainConnector()

    # 3. Discovered plugin memory connector
    if memory_type not in discovered_memory_plugins:
        available_plugins = (
            ", ".join(plugin_registry.list_available("agent_os.memory_connectors"))
            or "none"
        )
        raise WorkspaceLoadError(
            f"Unknown memory connector '{memory_type}'. "
            f"Available built-ins: markdown, gbrain. Discovered plugins: {available_plugins}."
        )

    try:
        plugin_item = plugin_registry.resolve(
            "agent_os.memory_connectors", memory_type
        )
    except PluginError as exc:
        raise WorkspaceLoadError(
            f"Failed to load memory connector plugin '{memory_type}': {exc}"
        ) from exc

    # Minimal supported entry-point contract:
    # - Instance: object implementing MemoryConnector interface (name, search, read_note)
    # - Class / Factory: callable accepting declared memory config parameters or 0 arguments
    if (
        not isinstance(plugin_item, type)
        and hasattr(plugin_item, "name")
        and callable(getattr(plugin_item, "search", None))
        and callable(getattr(plugin_item, "read_note", None))
    ):
        connector = plugin_item
    elif isinstance(plugin_item, type) or callable(plugin_item):
        sig = inspect.signature(plugin_item)
        params = sig.parameters
        kwargs: dict[str, Any] = {}
        missing_required: list[str] = []

        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = dict(ws.memory)
        else:
            for p in params.values():
                if p.kind in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                ):
                    continue
                if p.name in ws.memory:
                    kwargs[p.name] = ws.memory[p.name]
                elif p.default is not inspect.Parameter.empty:
                    continue
                else:
                    missing_required.append(p.name)

        if missing_required:
            raise WorkspaceLoadError(
                f"Memory connector plugin '{memory_type}' in group 'agent_os.memory_connectors' requires parameter(s) "
                f"{missing_required} which are not provided in workspace memory configuration."
            )

        try:
            connector = plugin_item(**kwargs)
        except Exception as exc:
            raise WorkspaceLoadError(
                f"Failed to instantiate memory connector plugin '{memory_type}': {exc}"
            ) from exc
    else:
        connector = plugin_item

    required_methods = (
        "search",
        "read_note",
        "list_notes",
        "write_note",
        "describe_write_side_effect",
    )
    if not (
        hasattr(connector, "name")
        and isinstance(connector.name, str)
        and hasattr(connector, "supported_write_modes")
        and all(callable(getattr(connector, method, None)) for method in required_methods)
    ):
        raise WorkspaceLoadError(
            f"Memory connector plugin '{memory_type}' does not satisfy the MemoryConnector interface "
            "(requires read methods plus WritableMemory write methods)."
        )

    return connector


def _validate_connector_references(
    ws: Workspace,
    registry: ConnectorRegistry,
) -> None:
    for connector in ws.connectors:
        try:
            registry.resolve(connector)
        except ValueError as exc:
            available = ", ".join(registry.list_connectors()) or "none"
            raise WorkspaceLoadError(
                f"Unknown workspace connector '{connector}'. Available connectors: {available}."
            ) from exc


def _workspace_environment(ws: Workspace) -> dict[str, str | None]:
    env: dict[str, str | None] = {}
    role_to_env = {
        "router": "LLM_ROUTER",
        "architect": "LLM_ARCHITECT",
        "executor": "LLM_EXECUTOR",
    }
    for role, env_name in role_to_env.items():
        if role not in ws.backends:
            continue
        value = ws.backends[role]
        if value is None:
            env[env_name] = None
            continue
        normalized = value.strip()
        if role in ("architect", "executor") and not normalized.startswith("cli/"):
            normalized = f"cli/{normalized}"
        env[env_name] = normalized

    memory_type = (
        str(ws.memory.get("type", ws.memory.get("connector", "markdown")))
        .strip()
        .lower()
    )
    if memory_type in ("markdown", "markdown_vault"):
        root = ws.memory.get(
            "path", ws.memory.get("root_path", ws.memory.get("root", "."))
        )
        env["AGENT_OS_MEMORY_CONNECTOR"] = "markdown"
        env["AGENT_OS_VAULT_PATH"] = _resolve_relative_path(ws.base_path, str(root))
    elif memory_type == "gbrain":
        env["AGENT_OS_MEMORY_CONNECTOR"] = "gbrain"
    else:
        env["AGENT_OS_MEMORY_CONNECTOR"] = memory_type

    return env


@contextmanager
def _patched_environment(values: dict[str, str | None]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _optional_string_list(value: Any, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkspaceLoadError(f"{field_name} must be a list of strings.")
    return value


def _looks_like_path(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or value.endswith(".toml")
    )
