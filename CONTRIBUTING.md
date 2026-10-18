# Contributing

Small, focused issues and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/simon-aibc/agentOS.git
cd agentOS
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,serve]"
```

## Required checks

Run the same offline checks used by CI:

```bash
python -m ruff check .
python -m pytest -W error
python -m pip check
git diff --check
```

The default suite must not require provider credentials, authenticated CLI
tools, Ollama, or live MCP servers. Mark genuinely external tests with
`@pytest.mark.integration` and document how to enable them.

## Pull requests

- Keep one behavioral or documentation concern per pull request.
- Add a regression test for bug fixes.
- Document new environment variables in `.env.example`.
- Preserve the public/private boundary: do not commit credentials, checkpoints,
  sandbox files, personal vault content, or real workflow logs.
- Explain any change to write permissions, subprocess execution, structured
  output, or checkpoint deserialization as a security-boundary change.

Report security issues privately according to [SECURITY.md](SECURITY.md).

## Preparing a public candidate

Follow [the release scope](docs/public-release.md). Export an explicit source
revision plus reviewed changes; do not package a developer's working directory.
Keep private application changes outside the generic Core candidate. Record the
exact file manifest, dependency inventory, test scope, and remaining gates.
Never infer permission to publish from a successful local build.
