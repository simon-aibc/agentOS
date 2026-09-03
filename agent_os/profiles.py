import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_os.backends import BackendRegistry


class ProfileLoadError(ValueError):
    """Raised for invalid, unsafe, or unresolvable profile configuration."""


class HotContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_chars: int = 8000
    max_age_days: int = 14
    sources: list[str] = Field(default_factory=lambda: ["hot.md", "AI/Memory/*.md"])


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold_tokens: int = 8000
    keep_recent_n: int = 6
    model: str | None = None


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    router: str | None = None
    architect: str | None = None
    executor: str | None = None
    sandbox: str | None = None
    description: str = ""
    extends: str | None = None
    hot_context: HotContextConfig | None = None
    summary: SummaryConfig | None = None


class ProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str | None = None
    profiles: dict[str, Profile] = {}


class ResolvedProfile(BaseModel):
    name: str
    router: str | None = None
    architect: str | None = None
    executor: str | None = None
    sandbox: str
    hot_context: HotContextConfig
    summary: SummaryConfig


def _reject_secrets(data: Any, path: str = "") -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = k.lower().replace("-", "_")
            if any(
                secret_word in k_lower
                for secret_word in (
                    "api_key",
                    "apikey",
                    "token",
                    "secret",
                    "password",
                    "credential",
                )
            ):
                raise ProfileLoadError(
                    f"Profile configuration contains rejected secret key at '{path}.{k}'"
                )
            _reject_secrets(v, f"{path}.{k}" if path else k)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _reject_secrets(item, f"{path}[{i}]")


def load_profiles(path: Path | None = None) -> ProfileFile:
    if path is not None:
        target_path = path
    else:
        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            target_path = Path(xdg_config) / "agent-os" / "profiles.toml"
        else:
            target_path = Path.home() / ".config" / "agent-os" / "profiles.toml"

        if not target_path.exists() and xdg_config is None:
            # Check both? The instruction says:
            # 2. $XDG_CONFIG_HOME/agent-os/profiles.toml
            # 3. ~/.config/agent-os/profiles.toml
            # First existing file wins.
            p2 = Path.home() / ".config" / "agent-os" / "profiles.toml"
            target_path = p2

    # Let's resolve the exact search paths
    search_paths = []
    if path is not None:
        search_paths.append(path)
    else:
        if xdg_config := os.getenv("XDG_CONFIG_HOME"):
            search_paths.append(Path(xdg_config) / "agent-os" / "profiles.toml")
        search_paths.append(Path.home() / ".config" / "agent-os" / "profiles.toml")

    found_path = None
    for p in search_paths:
        if p.exists():
            found_path = p
            break

    if found_path is None:
        return ProfileFile()

    try:
        with open(found_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ProfileLoadError(f"Malformed TOML in profiles file: {e}") from e

    _reject_secrets(data)

    # Inject name into Profile
    if "profile" in data and isinstance(data["profile"], dict):
        for name, prof_data in data["profile"].items():
            if isinstance(prof_data, dict):
                prof_data["name"] = name
        # Restructure so it maps to 'profiles' for Pydantic
        data["profiles"] = data.pop("profile")

    try:
        return ProfileFile.model_validate(data)
    except Exception as e:
        raise ProfileLoadError(f"Invalid profile configuration: {e}") from e


def select_profile_name(
    cli_name: str | None,
    env_name: str | None,
    file_default: str | None,
) -> tuple[str | None, str | None]:
    if cli_name is not None:
        return cli_name, "flag"
    if env_name is not None:
        return env_name, "env"
    if file_default is not None:
        return file_default, "default"
    return None, None


def resolve_profile(
    profile_file: ProfileFile,
    name: str,
    registry: BackendRegistry,
    sandbox_root_absolute: Path,
) -> ResolvedProfile:
    if name not in profile_file.profiles:
        available = ", ".join(profile_file.profiles.keys()) or "none"
        raise ProfileLoadError(
            f"Profile '{name}' not found. Available profiles: {available}"
        )

    # Build inheritance chain
    chain = []
    current_name: str | None = name
    while current_name:
        if current_name in chain:
            cycle = " -> ".join(chain + [current_name])
            raise ProfileLoadError(f"Inheritance cycle detected: {cycle}")

        if current_name not in profile_file.profiles:
            raise ProfileLoadError(f"Parent profile '{current_name}' not found.")

        if len(chain) > 1:
            raise ProfileLoadError(
                f"Deep inheritance is not supported: {' -> '.join(chain + [current_name])}"
            )

        chain.append(current_name)
        current_prof = profile_file.profiles[current_name]
        current_name = current_prof.extends

    # Resolve fields (child overrides parent)
    resolved_data = {}

    # Base from parent if exists
    if len(chain) > 1:
        parent = profile_file.profiles[chain[-1]]
        for field in ("router", "architect", "executor", "sandbox", "hot_context"):
            resolved_data[field] = getattr(parent, field)

    # Apply child overrides
    child = profile_file.profiles[name]
    child_set = child.model_fields_set
    for field in (
        "router",
        "architect",
        "executor",
        "sandbox",
        "hot_context",
        "summary",
    ):
        if field in child_set:
            resolved_data[field] = getattr(child, field)

    # Resolve sandbox
    sandbox_str = resolved_data.get("sandbox")
    if sandbox_str is not None:
        resolved_sandbox = Path(sandbox_str).expanduser().resolve()
    else:
        resolved_sandbox = sandbox_root_absolute

    resolved_data["sandbox"] = str(resolved_sandbox)

    # Validate backends
    for role in ("architect", "executor"):
        backend_str = resolved_data.get(role)
        if backend_str and backend_str.startswith("cli/"):
            backend_name = backend_str[4:]
            try:
                registry.resolve(role, backend_name)
            except ValueError as e:
                raise ProfileLoadError(str(e)) from e

    router_str = resolved_data.get("router")
    if router_str and router_str.startswith("cli/"):
        raise ProfileLoadError(f"Router cannot be a CLI backend: {router_str}")

    return ResolvedProfile(
        name=name,
        router=resolved_data.get("router"),
        architect=resolved_data.get("architect"),
        executor=resolved_data.get("executor"),
        sandbox=resolved_data["sandbox"],
        hot_context=resolved_data.get("hot_context") or HotContextConfig(),
        summary=resolved_data.get("summary") or SummaryConfig(),
    )
