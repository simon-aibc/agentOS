import hashlib
import sys
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from langchain_core.tools import BaseTool

from agent_os.plugins import PluginError, PluginRegistry
from agent_os.skills import RegisteredSkill, SkillHandler, SkillRegistry


class SkillPackageLoader:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def load_from_directories(self, directories: list[str]) -> None:
        """Load skill packages from a list of directories."""
        for root_dir in directories:
            path = Path(root_dir)
            if not path.is_dir():
                continue

            for entry in sorted(path.iterdir()):
                if entry.is_dir():
                    self.load_package(entry)

    def load_package(self, package_dir: Path) -> None:
        """Load a single skill package from its directory."""
        manifest_path = package_dir / "manifest.toml"
        if not manifest_path.is_file():
            raise ValueError(f"Skill manifest not found: {manifest_path}")

        try:
            with manifest_path.open("rb") as manifest_file:
                manifest = tomllib.load(manifest_file)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"Failed to parse manifest {manifest_path}: {e}") from e

        skill_metadata = manifest.get("skill")
        if not isinstance(skill_metadata, dict):
            raise ValueError(f"Missing [skill] table in {manifest_path}")
        for field in ("name", "version"):
            value = skill_metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"[skill].{field} must be non-empty in {manifest_path}"
                )

        handlers = skill_metadata.get("handlers")
        if not isinstance(handlers, list) or not handlers:
            raise ValueError(
                f"[skill] must define at least one handler in {manifest_path}"
            )

        loaded: list[RegisteredSkill] = []
        for handler_conf in handlers:
            if not isinstance(handler_conf, dict):
                raise ValueError(f"Handler entries must be tables in {manifest_path}")
            entrypoint = handler_conf.get("entrypoint")
            if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
                raise ValueError(
                    f"Invalid entrypoint {entrypoint!r} in {manifest_path}; "
                    "expected 'module:function'"
                )

            module_name, func_name = entrypoint.split(":")
            module_parts = module_name.split(".")
            if (
                not module_name
                or not all(part.isidentifier() for part in module_parts)
                or not func_name.isidentifier()
            ):
                raise ValueError(
                    f"Invalid entrypoint {entrypoint!r} in {manifest_path}; "
                    "expected 'module:function'"
                )
            module_path = package_dir.joinpath(*module_parts).with_suffix(".py")
            if not module_path.is_file():
                raise ValueError(
                    f"Module {module_path} not found for {entrypoint} in {manifest_path}"
                )

            digest = hashlib.sha256(str(package_dir.resolve()).encode()).hexdigest()[
                :16
            ]
            import_name = f"_agent_os_skill_{digest}_{'_'.join(module_parts)}"
            spec = spec_from_file_location(import_name, module_path)
            if not spec or not spec.loader:
                raise ValueError(f"Failed to load {module_path} from {manifest_path}")

            module = module_from_spec(spec)
            sys.modules[import_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                sys.modules.pop(import_name, None)
                raise ValueError(
                    f"Failed to load {entrypoint} from {manifest_path}: {e}"
                ) from e

            if not hasattr(module, func_name):
                raise ValueError(
                    f"Function {func_name} not found in {module_path} ({manifest_path})"
                )

            handler: SkillHandler = getattr(module, func_name)
            if not isinstance(handler, BaseTool) and not callable(handler):
                raise ValueError(
                    f"Handler {entrypoint} is not callable in {manifest_path}"
                )

            matches = handler_conf.get("match")
            if (
                not isinstance(matches, list)
                or not matches
                or not all(
                    isinstance(match, str) and match.strip() for match in matches
                )
            ):
                raise ValueError(
                    f"Handler {entrypoint} needs non-empty match values in {manifest_path}"
                )

            loaded.append(
                RegisteredSkill(
                    name=matches[0],
                    aliases=matches[1:],
                    handler=handler,
                )
            )

        self.registry.register_many(loaded)

    def load_from_entry_points(
        self, plugin_registry: PluginRegistry | None = None
    ) -> None:
        """Load skill packages discovered via 'agent_os.skill_packages' entry points."""
        if plugin_registry is None:
            plugin_registry = PluginRegistry()

        try:
            discovered = plugin_registry.discover("agent_os.skill_packages")
        except PluginError as exc:
            raise ValueError(
                f"Plugin discovery failed for group 'agent_os.skill_packages': {exc}"
            ) from exc

        for ep_name in discovered:
            try:
                target = plugin_registry.resolve("agent_os.skill_packages", ep_name)
            except PluginError as exc:
                raise ValueError(
                    f"Failed to load skill package plugin '{ep_name}': {exc}"
                ) from exc

            # Supported targets:
            # 1. Path or str pointing to a skill package directory with manifest.toml
            if isinstance(target, (str, Path)):
                path = Path(target)
                if path.is_dir() and (path / "manifest.toml").is_file():
                    self.load_package(path)
                else:
                    raise ValueError(
                        f"Skill package plugin '{ep_name}' path '{target}' is not a valid skill package directory."
                    )
            # 2. RegisteredSkill instance
            elif isinstance(target, RegisteredSkill):
                self.registry.register(target)
            # 3. Iterable of RegisteredSkill instances
            elif isinstance(target, (list, tuple)) and all(
                isinstance(item, RegisteredSkill) for item in target
            ):
                self.registry.register_many(list(target))
            # 4. Zero-argument factory callable
            elif callable(target):
                try:
                    result = target()
                except Exception as exc:
                    raise ValueError(
                        f"Failed to instantiate skill package plugin '{ep_name}': {exc}"
                    ) from exc

                if isinstance(result, (str, Path)):
                    path = Path(result)
                    if path.is_dir() and (path / "manifest.toml").is_file():
                        self.load_package(path)
                    else:
                        raise ValueError(
                            f"Skill package factory '{ep_name}' returned invalid path '{result}'."
                        )
                elif isinstance(result, RegisteredSkill):
                    self.registry.register(result)
                elif isinstance(result, (list, tuple)) and all(
                    isinstance(item, RegisteredSkill) for item in result
                ):
                    self.registry.register_many(list(result))
                else:
                    raise ValueError(
                        f"Skill package plugin '{ep_name}' factory returned unsupported type {type(result)}."
                    )
            else:
                raise ValueError(
                    f"Skill package plugin '{ep_name}' returned unsupported object of type {type(target)}."
                )
