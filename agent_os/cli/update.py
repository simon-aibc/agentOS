"""CLI handler for `agent-os update`.

Supports:
- Dry-run update check (`--check`)
- Safe pre-update DB backups
- Docker container detection vs Pip/Wheel environment
- Optional automated execution with `--yes`, `--pull`, and `--reload`
- Daemon reload guidance
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from importlib.metadata import Distribution
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from rich.console import Console

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.migrations import (
    PERMISSIONS_MIGRATIONS,
    RUNS_MIGRATIONS,
    backup_database,
    run_migrations,
)
from agent_os.observations import DEFAULT_OBSERVATION_DB, OBSERVATION_DB_ENV
from agent_os.permission_store import DEFAULT_PERMISSION_DB, PERMISSION_DB_ENV
from agent_os.runs import _get_db_path as get_runs_db_path
from agent_os.schedules import _get_db_path as get_sched_db_path
from agent_os.update_check import check_for_update

logger = logging.getLogger(__name__)


def is_docker_environment() -> bool:
    """Return True if running inside a Docker container."""
    if Path("/.dockerenv").exists():
        return True
    if Path("/run/.containerenv").exists():
        return True
    if os.getenv("CONTAINER") == "docker":
        return True
    return False


PACKAGE_NAME = "agent-os-langgraph"
REPO_URL = "https://github.com/simon-aibc/agent-os-langgraph"


def resolve_source_checkout() -> Path | None:
    """Return the checkout root when this package is running from source.

    Two signals, in order of authority:

    1. PEP 610 ``direct_url.json``, which pip writes for ``pip install -e .``
       and for local-path installs, and which points at a directory on this
       machine.
    2. A git checkout containing this package: metadata resolution can land on
       a stale ``*.egg-info`` (which carries no ``direct_url.json``) whenever the
       repository root precedes site-packages on ``sys.path``, so fall back to
       the location of the imported package itself.

    Returns ``None`` for a normal index install, where upgrading via pip is right.
    """
    try:
        dist = Distribution.from_name(PACKAGE_NAME)
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            url = data.get("url", "")
            if url.startswith("file://"):
                path = Path(url2pathname(unquote(urlparse(url).path)))
                if path.is_dir():
                    return path
    except Exception:
        pass

    try:
        root = Path(__file__).resolve().parent.parent.parent
        if (root / ".git").exists() and (root / "pyproject.toml").is_file():
            return root
    except Exception:
        pass
    return None


def run_pre_update_db_backups() -> list[str]:
    """Execute pre-migration checks and backups on all known SQLite databases."""
    db_configs: list[tuple[str, tuple[Any, ...]]] = [
        (get_runs_db_path(), RUNS_MIGRATIONS),
        (get_sched_db_path(), ()),
        (os.getenv(PERMISSION_DB_ENV, DEFAULT_PERMISSION_DB), PERMISSIONS_MIGRATIONS),
        (os.getenv(OBSERVATION_DB_ENV, DEFAULT_OBSERVATION_DB), ()),
    ]

    checkpoint_db = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    if checkpoint_db != ":memory:":
        db_configs.append((checkpoint_db, ()))

    backed_up: list[str] = []
    for db_path_str, migrations in db_configs:
        if db_path_str == ":memory:":
            continue
        p = Path(db_path_str).expanduser()
        if p.exists() and p.is_file():
            try:
                # 1. Explicitly backup database before update
                backup_path = backup_database(p)
                if backup_path and backup_path.exists():
                    backed_up.append(str(p))
            except Exception as exc:
                logger.warning(f"Could not create pre-update backup for {p}: {exc}")
            try:
                # 2. Run schema migrations
                run_migrations(p, migrations=migrations, baseline_version=1)
            except Exception as exc:
                logger.warning(f"Pre-update migration check failed for {p}: {exc}")
    return backed_up


def handle_update_command(
    args: argparse.Namespace, console: Console | None = None
) -> int:
    """Entrypoint for the `agent-os update` command."""
    out = console or Console()
    force_check = getattr(args, "check", False) or getattr(args, "force", False)
    info = check_for_update(force=force_check)

    out.print(f"[bold]Current Version:[/bold] {info.current_version}")
    out.print(f"[bold]Latest Version:[/bold]  {info.latest_version}")

    if getattr(args, "check", False):
        if info.update_available:
            out.print(
                f"[bold green]Update available![/bold green] ({info.current_version} -> {info.latest_version})"
            )
            if info.release_url:
                out.print(f"Release URL: {info.release_url}")
        else:
            out.print("[green]agent-os is up to date.[/green]")
        return 0

    if not info.update_available and not getattr(args, "force", False):
        out.print("[green]agent-os is already up to date.[/green]")
        return 0

    out.print("\n[bold cyan]1. Preparing database backup...[/bold cyan]")
    backed_up = run_pre_update_db_backups()
    if backed_up:
        for db in backed_up:
            out.print(f"  ✓ Verified/backed up database: {db}")
    else:
        out.print("  ✓ No active database files needed backup.")

    in_docker = is_docker_environment()
    yes_flag = getattr(args, "yes", False)

    out.print("\n[bold cyan]2. Update Execution[/bold cyan]")
    if in_docker:
        out.print("[yellow]Detected Docker container runtime.[/yellow]")
        out.print("To update your container, run on host:")
        out.print("  [bold]docker compose pull && docker compose up -d[/bold]\n")
        if yes_flag and getattr(args, "pull", False):
            out.print("Executing `docker compose pull`...")
            try:
                subprocess.run(["docker", "compose", "pull"], check=False)
            except Exception as e:
                out.print(f"[red]Failed to execute docker compose pull: {e}[/red]")
    else:
        checkout = resolve_source_checkout()
        if checkout is not None:
            # Upgrading a source checkout through an index would detach it from
            # the working tree it is installed from, so guide git instead. Never
            # run this automatically: `git pull` can conflict or discard local work.
            out.print("[yellow]Detected source checkout (editable install).[/yellow]")
            out.print(f"Checkout: [bold]{checkout}[/bold]")
            out.print("To upgrade, pull and reinstall from the checkout:")
            out.print(f"  [bold]git -C {checkout} pull --ff-only[/bold]")
            out.print(
                f"  [bold]{sys.executable} -m pip install -e {checkout}[/bold]\n"
            )
            if yes_flag:
                out.print(
                    "[yellow]`--yes` does not auto-upgrade a source checkout: "
                    "pulling could conflict with local changes, and installing "
                    "from an index would replace your editable install.[/yellow]"
                )
        else:
            out.print("[yellow]Detected pip / local Python runtime.[/yellow]")
            # This package is not published to PyPI, so `pip install --upgrade
            # agent-os-langgraph` cannot resolve. Install from the Git remote.
            target = f"{PACKAGE_NAME} @ git+{REPO_URL}"
            if info.latest_version:
                target = f"{target}@v{info.latest_version.lstrip('v')}"
            upgrade_cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                target,
            ]
            if yes_flag:
                out.print(f"Running: {' '.join(upgrade_cmd)}")
                res = subprocess.run(upgrade_cmd, check=False)
                if res.returncode != 0:
                    out.print(
                        f"[bold red]Upgrade failed with exit code {res.returncode}[/bold red]"
                    )
                    return res.returncode
                out.print(
                    "[bold green]Successfully upgraded agent-os-langgraph![/bold green]"
                )
            else:
                out.print("To upgrade, execute:")
                out.print(f'  [bold]pip install --upgrade "{target}"[/bold]')
                out.print("Or re-run with `--yes`:")
                out.print("  [bold]agent-os update --yes[/bold]")

    out.print("\n[bold cyan]3. Service Reload[/bold cyan]")
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    kickstart_cmd = [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{uid}/com.simon.agentos",
    ]
    if getattr(args, "reload", False):
        out.print(f"Reloading daemon: {' '.join(kickstart_cmd)}")
        try:
            subprocess.run(kickstart_cmd, check=False)
        except Exception as e:
            out.print(f"[yellow]Daemon reload note: {e}[/yellow]")
    else:
        out.print("If running as a background service, restart with:")
        out.print(f"  [bold]launchctl kickstart -k gui/{uid}/com.simon.agentos[/bold]")
        out.print("Or restart the server manually:")
        out.print("  [dim]pkill -f 'agent-os serve' && agent-os serve[/dim]\n")

    return 0
