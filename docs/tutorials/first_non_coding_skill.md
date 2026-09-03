# Build your first non-coding skill

This tutorial demonstrates how to build a non-coding skill for `agent-os` using the connector framework introduced in v1.3.

## Example: vault_qa

`vault_qa` is an example skill that answers questions using your Second Brain (Markdown Vault) via the `MemoryConnector` interface.

### 1. Create the Manifest

Create `skills/vault_qa/manifest.toml`:
```toml
[skill]
name = "vault_qa"
version = "0.1.0"
description = "Hỏi đáp kiến thức từ Second Brain (Vault) thông qua MemoryConnector"

[[skill.handlers]]
match = ["hỏi vault", "vault qa", "tra cứu"]
entrypoint = "handlers:vault_qa"
connectors = ["memory"]
```

### 2. Create the Handler

Create `skills/vault_qa/handlers.py`:
```python
from agent_os.connectors import MarkdownVaultConnector


def vault_qa(task: str, **kwargs) -> str:
    # 1. Parse query
    query = task.replace("hỏi vault:", "").strip()

    # 2. Setup connector (in real usage, this might be injected or configured)
    connector = MarkdownVaultConnector("./sandbox")

    # 3. Search and format
    results = connector.search(query)

    if not results:
        return f"Không tìm thấy thông tin cho: {query}"

    out = [f"Kết quả cho '{query}':"]
    for r in results:
        out.append(f"- {r['path']}")
    return "\n".join(out)
```

### 3. Usage

Run your skill through the agent-os CLI using deterministic matching (zero LLM token cost for routing):

```bash
agent-os "hỏi vault: <câu hỏi của bạn>"
```

The system will route the query directly to your `vault_qa` handler and execute it using the `MemoryConnector` without touching the generic LLM coding path.
