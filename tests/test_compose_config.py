import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CONSOLE_DIGEST = (
    "sha256:1ee8c32becb93212c10fa74bdb0b74329187f02464e97353e522bd389fcb7ebb"
)


def _config() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_compose_file_exists():
    assert COMPOSE_FILE.is_file()


def test_compose_ports_loopback():
    for service_name, service in _config()["services"].items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (
                f"{service_name} port {port} is not bound to loopback"
            )


def test_compose_console_digest_pinned():
    console = _config()["services"]["console"]
    image_name, separator, digest = console["image"].partition("@")

    assert image_name == "ghcr.io/simon-aibc/agent-os-console"
    assert separator == "@"
    assert digest == CONSOLE_DIGEST
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)


def test_compose_health_dependencies():
    console = _config()["services"]["console"]
    assert console["depends_on"]["backend"]["condition"] == "service_healthy"


def test_compose_console_runtime_contract():
    console = _config()["services"]["console"]
    environment = console["environment"]

    assert environment == [
        "AGENT_OS_API_BASE=${AGENT_OS_API_BASE:-http://127.0.0.1:4680}",
        "HOSTNAME=0.0.0.0",
    ]
    assert console["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -qO- http://127.0.0.1:4100/ >/dev/null || exit 1",
    ]


def test_compose_backend_environment_contract():
    backend = _config()["services"]["backend"]
    environment = set(backend["environment"])

    assert {
        "LLM_ROUTER",
        "LLM_ARCHITECT",
        "LLM_EXECUTOR",
        "AGENT_OS_CORS_ORIGINS",
        "AGENT_OS_CHECKPOINTS_DB=/data/checkpoints.db",
        "AGENT_OS_SCHED_DB=/data/checkpoints.sched.db",
        "AGENT_OS_SCHEDULER_ENABLED",
        "AGENT_OS_SCHED_TICK_SECONDS",
        "AGENT_OS_SANDBOX=/workspace",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    } <= environment


def test_compose_no_secret_literals():
    content = COMPOSE_FILE.read_text(encoding="utf-8")
    forbidden = ["sk-", "AIza", "ghp_", "github_pat_", "-----BEGIN PRIVATE KEY"]
    assert not any(marker in content for marker in forbidden)


def test_compose_volumes_and_persistent_paths():
    backend = _config()["services"]["backend"]
    volumes = backend["volumes"]

    assert "agentos_data:/data" in volumes
    assert "${AGENT_OS_WORKSPACE:-.}:/workspace" in volumes
