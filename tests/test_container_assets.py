from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def test_dockerfile_exists():
    assert DOCKERFILE.exists()


def test_dockerignore_exists():
    assert DOCKERIGNORE.exists()


def test_dockerfile_non_root_user():
    content = DOCKERFILE.read_text()
    assert "\nUSER agentos" in content or "\nUSER " in content
    assert "useradd " in content


def test_dockerfile_serve_extra():
    content = DOCKERFILE.read_text()
    assert 'pip install --no-cache-dir "${WHEEL}[serve]"' in content
    assert "WHEEL=$(ls /tmp/wheel/*.whl | head -n 1)" in content


def test_dockerfile_pinned_digest():
    content = DOCKERFILE.read_text()
    assert (
        "FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
        in content
    )


def test_dockerfile_multi_stage():
    content = DOCKERFILE.read_text()
    assert " AS builder" in content
    assert "COPY --from=builder" in content


def test_dockerfile_env_defaults():
    content = DOCKERFILE.read_text()
    assert "ENV AGENT_OS_CHECKPOINTS_DB=/data/checkpoints.db" in content
    assert "ENV AGENT_OS_VAULT_PATH=/workspace" in content
    assert "ENV AGENT_OS_SANDBOX=/workspace" in content


def test_dockerfile_healthcheck():
    content = DOCKERFILE.read_text()
    assert "HEALTHCHECK" in content
    assert "/api/health" in content


def test_dockerfile_no_secrets():
    content = DOCKERFILE.read_text()
    # Simple static checks to avoid baking secrets
    forbidden = ["ENV OPENAI_API_KEY", "ENV ANTHROPIC_API_KEY", "COPY .env"]
    for word in forbidden:
        assert word not in content


def test_dockerignore_excludes_secrets():
    content = DOCKERIGNORE.read_text()
    assert ".env*" in content
    assert "!.env.example" in content
    assert "*.sqlite" in content or "*.db" in content
    assert "sandbox/" in content
    assert ".claude/" in content
    assert ".sandbox/" in content
    assert ".DS_Store" in content
