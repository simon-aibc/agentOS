import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from agent_os import profiles as profile_config
from agent_os.backends import build_default_registry
from agent_os.checkpoints import DEFAULT_CHECKPOINT_DB
from agent_os.sandbox import get_sandbox_root
from agent_os.state import BackendBinding


def run_doctor(
    json_output: bool,
    workspace_path: str | None = None,
) -> tuple[int, str]:
    warnings: list[str] = []
    exit_code = 0

    registry = build_default_registry()
    adapters_info: list[dict[str, Any]] = []

    for _, adapter in registry.items():
        is_stub = getattr(adapter, "stub", False)
        if is_stub:
            binary_path = None
            status_obj = adapter.authentication_status()
            auth_status = {
                "status": status_obj.status,
                "detail": status_obj.detail,
            }
        else:
            # Binary check without invoking the adapter.
            import shutil

            binary_path = shutil.which(adapter.binary_name)
            if binary_path is None:
                auth_status = {
                    "status": "unknown",
                    "detail": f"Binary '{adapter.binary_name}' not found on PATH.",
                }
            else:
                status_obj = adapter.authentication_status()
                auth_status = {
                    "status": status_obj.status,
                    "detail": status_obj.detail,
                }

        adapters_info.append(
            {
                "name": adapter.name,
                "binary_name": adapter.binary_name,
                "supported_roles": sorted(adapter.supported_roles),
                "binary_path": binary_path,
                "auth_status": auth_status,
                "stub": is_stub,
            }
        )

    profile_name_val = None
    profile_source = None
    resolved_profile = None
    workspace_binding = None
    effective_workspace = workspace_path or os.getenv("AGENT_OS_WORKSPACE")
    if effective_workspace:
        try:
            from agent_os.workspace import load_workspace

            workspace = load_workspace(effective_workspace)
            configured_backends = dict(workspace.backends)
            workspace_binding = BackendBinding(
                router=configured_backends.get("router"),
                architect=_normalize_workspace_backend(configured_backends.get("architect")),
                executor=_normalize_workspace_backend(configured_backends.get("executor")),
                profile_name=None,
                sandbox_root=str(get_sandbox_root().resolve()),
            )
        except Exception as error:
            warnings.append(f"Workspace error: {error}")
            exit_code = 1
    else:
        try:
            profile_file = profile_config.load_profiles()
            profile_name_val, profile_source = profile_config.select_profile_name(
                None,
                os.getenv("AGENT_OS_PROFILE"),
                profile_file.default,
            )
            if profile_name_val is not None:
                resolved_profile = profile_config.resolve_profile(
                    profile_file,
                    profile_name_val,
                    registry,
                    get_sandbox_root().resolve(),
                )
        except Exception as error:
            warnings.append(f"Profile error: {error}")
            exit_code = 1

    if workspace_binding is not None:
        resolved_config = {
            "router": workspace_binding.router,
            "architect": workspace_binding.architect,
            "executor": workspace_binding.executor,
            "sandbox": workspace_binding.sandbox_root,
        }
        profile_name_val = workspace_binding.profile_name
        profile_source = "workspace"
    elif resolved_profile is None:
        resolved_config = {
            "router": os.getenv("LLM_ROUTER"),
            "architect": os.getenv("LLM_ARCHITECT"),
            "executor": os.getenv("LLM_EXECUTOR"),
            "sandbox": str(get_sandbox_root().resolve()),
        }
    else:
        resolved_config = {
            "router": resolved_profile.router,
            "architect": resolved_profile.architect,
            "executor": resolved_profile.executor,
            "sandbox": resolved_profile.sandbox,
        }

    checkpoint_env = os.getenv("AGENT_OS_CHECKPOINTS_DB")
    cp_path_str = (
        checkpoint_env if checkpoint_env is not None else DEFAULT_CHECKPOINT_DB
    )
    cp_path = Path(cp_path_str).resolve()

    cp_exists = cp_path.exists()
    cp_writable = False

    if cp_exists:
        cp_writable = os.access(cp_path, os.W_OK)
        try:
            conn = sqlite3.connect(f"file:{cp_path}?mode=ro", uri=True)
            conn.close()
        except Exception as e:
            warnings.append(f"Checkpoint DB is not readable: {e}")
    else:
        cp_writable = os.access(cp_path.parent, os.W_OK)
        if not cp_writable:
            warnings.append(
                f"Checkpoint DB parent directory is not writable: {cp_path.parent}"
            )

    if not cp_writable:
        warnings.append("Checkpoint DB is not writable.")
        exit_code = 1

    checkpoints_db = {
        "path": str(cp_path),
        "exists": cp_exists,
        "writable": cp_writable,
    }
    backend_binding = BackendBinding(
        router=resolved_config["router"],
        architect=resolved_config["architect"],
        executor=resolved_config["executor"],
        profile_name=profile_name_val,
        sandbox_root=resolved_config["sandbox"],
    )

    # Validate configured config
    for role in ["architect", "executor"]:
        backend_str = resolved_config[role]
        if backend_str and backend_str.startswith("cli/"):
            backend_name = backend_str[4:]

            adapter_info = next(
                (a for a in adapters_info if a["name"] == backend_name), None
            )
            if not adapter_info:
                warnings.append(
                    f"Configured {role} backend '{backend_str}' is not registered."
                )
                exit_code = 1
                continue

            if adapter_info["stub"]:
                warnings.append(
                    f"Configured {role} backend '{backend_name}' is a "
                    "not-yet-supported stub; workflows will fail on invocation."
                )
                continue

            if role not in adapter_info["supported_roles"]:
                warnings.append(
                    f"Configured {role} backend '{backend_str}' does not support role '{role}'."
                )
                exit_code = 1

            if adapter_info["binary_path"] is None:
                warnings.append(
                    f"Configured {role} backend '{backend_str}' binary is missing."
                )
                exit_code = 1

            if adapter_info["auth_status"]["status"] == "unauthenticated":
                warnings.append(
                    f"Configured {role} backend '{backend_str}' is unauthenticated."
                )
                exit_code = 1
            elif adapter_info["auth_status"]["status"] == "unknown":
                warnings.append(
                    f"Configured {role} backend '{backend_str}' auth status is unknown."
                )

    router_backend = resolved_config["router"]
    if router_backend and router_backend.startswith("cli/"):
        router_name = router_backend[4:]
        router_info = next(
            (adapter for adapter in adapters_info if adapter["name"] == router_name),
            None,
        )
        if router_info and router_info["stub"]:
            warnings.append(
                f"Configured router backend '{router_name}' is a "
                "not-yet-supported stub; workflows will fail on invocation."
            )

    report = {
        "registered_adapters": adapters_info,
        "resolved_config": resolved_config,
        "profile": profile_name_val,
        "profile_source": profile_source,
        "checkpoints_db": checkpoints_db,
        "backend_binding": backend_binding.model_dump(),
        "warnings": warnings,
    }

    if json_output:
        return exit_code, json.dumps(report, indent=2)

    # Render human output
    lines = []
    lines.append("Registered adapters")
    lines.append("-------------------")
    if not adapters_info:
        lines.append("none")
    for ad in (item for item in adapters_info if not item["stub"]):
        lines.append(f"- {ad['name']} ({ad['binary_name']})")
        lines.append(f"  Roles : {', '.join(ad['supported_roles'])}")
        lines.append(f"  Binary: {ad['binary_path'] or 'missing'}")
        lines.append(
            f"  Auth  : {ad['auth_status']['status']} ({ad['auth_status']['detail']})"
        )

    candidates = [item for item in adapters_info if item["stub"]]
    if candidates:
        lines.append("")
        lines.append("Candidate adapters (not-yet-supported)")
        lines.append("--------------------------------------")
        for ad in candidates:
            lines.append(f"- {ad['name']}")
            lines.append(f"  Roles : {', '.join(ad['supported_roles'])}")
            lines.append(f"  Status: {ad['auth_status']['detail']}")

    lines.append("")
    lines.append("Resolved config")
    lines.append("---------------")
    for k, v in resolved_config.items():
        lines.append(f"{k.capitalize()}: {v if v is not None else 'null'}")

    lines.append("")
    if profile_name_val:
        lines.append(f"Profile: {profile_name_val} (via {profile_source})")
    else:
        lines.append("Profile: none")

    lines.append("")
    lines.append("Checkpoints DB")
    lines.append("--------------")
    lines.append(f"Path    : {checkpoints_db['path']}")
    lines.append(f"Exists  : {'yes' if checkpoints_db['exists'] else 'no'}")
    lines.append(f"Writable: {'yes' if checkpoints_db['writable'] else 'no'}")

    lines.append("")
    lines.append("Backend binding")
    lines.append("---------------")
    for key, value in backend_binding.model_dump().items():
        lines.append(f"{key}: {value if value is not None else 'null'}")

    lines.append("")
    lines.append("Warnings")
    lines.append("--------")
    if not warnings:
        lines.append("none")
    else:
        for w in warnings:
            lines.append(f"- {w}")

    lines.append("")
    if exit_code == 0:
        lines.append("STATUS: OK")
    else:
        lines.append("STATUS: FAIL")

    return exit_code, "\n".join(lines)


def _normalize_workspace_backend(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized if normalized.startswith("cli/") else f"cli/{normalized}"
