# Dependency inventory for Core source preparation

Observed 2026-10-18 on macOS, Python 3.12.13: installed Core wheel plus `dev` and `serve` extras. This is a resolved-environment snapshot, not a cross-platform lockfile, complete SBOM, vulnerability assessment, or blanket redistribution approval. The source and Core wheel do not bundle these dependency packages. Rebuild the inventory if dependencies or distribution format change.

The Core license remains [MIT](../LICENSE). Third-party components retain their own licenses and notices. Metadata and installed license files are evidence; inspect the actual files when packaging a complete environment, installer, appliance, or container.

## Previously missing metadata

Six distributions lacked both `License-Expression` and `License` fields. Installed classifiers/text identify aiosqlite, markdown-it-py and mdurl as MIT; Jinja2 and fastuuid have BSD-family classifiers and bundled license text; tokenizers has an Apache classifier and the [upstream v0.23.3 license](https://github.com/huggingface/tokenizers/blob/v0.23.3/LICENSE) is Apache-2.0. This closes the missing-identification question, not every legal review question.

98 of 101 installed distributions carry license/notice files found in wheel records. The installed wheels for `langchain-core`, `sqlite-vec`, and `tokenizers` lacked such files in this inspection. Keep this distinction visible before bundling dependencies into a full deployment.

## Observed versions

| Distribution | Version | Declared license / classifier | Bundled text observed |
|---|---|---|---|
| agent-os-langgraph | 2.4.0 | MIT | Yes |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | Yes |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | Yes |
| aiosignal | 1.4.0 | Apache 2.0 | Yes |
| aiosqlite | 0.22.1 | MIT License | Yes |
| annotated-doc | 0.0.5 | MIT | Yes |
| annotated-types | 0.8.0 | MIT | Yes |
| anyio | 4.14.2 | MIT | Yes |
| attrs | 26.1.0 | MIT | Yes |
| certifi | 2026.7.22 | MPL-2.0 | Yes |
| cffi | 2.1.1 | MIT-0 | Yes |
| charset-normalizer | 3.5.2 | MIT | Yes |
| click | 8.5.0 | BSD-3-Clause | Yes |
| croniter | 6.0.0 | MIT License | Yes |
| cryptography | 48.0.1 | Apache-2.0 OR BSD-3-Clause | Yes |
| distro | 1.9.0 | Apache License, Version 2.0 | Yes |
| fastapi | 0.144.0 | MIT | Yes |
| fastuuid | 0.14.0 | BSD License | Yes |
| filelock | 4.2.2 | MIT | Yes |
| frozenlist | 1.8.0 | Apache-2.0 | Yes |
| fsspec | 2026.9.0 | BSD-3-Clause | Yes |
| h11 | 0.16.0 | MIT | Yes |
| hf-xet | 1.7.0 | Apache-2.0 | Yes |
| httpcore | 1.0.9 | BSD-3-Clause | Yes |
| httpcore2 | 2.13.1 | BSD-3-Clause | Yes |
| httpx | 0.28.1 | BSD-3-Clause | Yes |
| httpx-sse | 0.4.3 | MIT | Yes |
| httpx2 | 2.13.1 | BSD-3-Clause | Yes |
| huggingface_hub | 2.2.0 | Apache-2.0 | Yes |
| idna | 3.20 | BSD-3-Clause | Yes |
| importlib_metadata | 8.9.0 | Apache-2.0 | Yes |
| iniconfig | 2.3.1 | MIT | Yes |
| Jinja2 | 3.1.6 | BSD License | Yes |
| jiter | 0.17.0 | MIT | Yes |
| jsonpatch | 1.35 | BSD-3-Clause | Yes |
| jsonpointer | 3.2.1 | Modified BSD License | Yes |
| jsonschema | 4.26.0 | MIT | Yes |
| jsonschema-specifications | 2025.9.1 | MIT | Yes |
| langchain | 1.3.14 | MIT | Yes |
| langchain-core | 1.5.3 | MIT | No |
| langchain-litellm | 0.7.0 | MIT | Yes |
| langchain-mcp-adapters | 0.3.1 | MIT | Yes |
| langchain-protocol | 0.0.19 | MIT | Yes |
| langgraph | 1.2.10 | MIT | Yes |
| langgraph-checkpoint | 4.3.0 | MIT | Yes |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT | Yes |
| langgraph-prebuilt | 1.1.1 | MIT | Yes |
| langgraph-sdk | 0.4.8 | MIT | Yes |
| langsmith | 0.14.11 | MIT | Yes |
| litellm | 1.95.0 | MIT | Yes |
| markdown-it-py | 4.2.0 | MIT License | Yes |
| MarkupSafe | 3.0.4 | BSD-3-Clause | Yes |
| mcp | 1.29.0 | MIT | Yes |
| mdurl | 0.1.2 | MIT License | Yes |
| multidict | 7.1.0 | Apache-2.0 | Yes |
| openai | 2.54.0 | Apache-2.0 | Yes |
| opentelemetry-api | 1.45.1 | Apache-2.0 | Yes |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | Yes |
| ormsgpack | 1.13.0 | Apache-2.0 OR MIT | Yes |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Yes |
| pluggy | 1.7.0 | MIT | Yes |
| propcache | 0.5.4 | Apache-2.0 | Yes |
| pycparser | 3.11 | BSD-3-Clause | Yes |
| pydantic | 2.13.4 | MIT | Yes |
| pydantic-settings | 2.16.0 | MIT | Yes |
| pydantic_core | 2.46.4 | MIT | Yes |
| Pygments | 2.21.0 | BSD-2-Clause | Yes |
| PyJWT | 2.15.1 | MIT | Yes |
| pytest | 9.1.1 | MIT | Yes |
| python-dateutil | 2.9.0.post0 | Dual License | Yes |
| python-dotenv | 1.2.2 | BSD-3-Clause | Yes |
| python-multipart | 0.0.32 | Apache-2.0 | Yes |
| pytz | 2026.5 | MIT | Yes |
| PyYAML | 6.0.3 | MIT | Yes |
| referencing | 0.37.0 | MIT | Yes |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python | Yes |
| requests | 2.34.2 | Apache-2.0 | Yes |
| requests-toolbelt | 1.0.0 | Apache 2.0 | Yes |
| rich | 15.0.0 | MIT | Yes |
| rpds-py | 2026.9.1 | MIT | Yes |
| ruff | 0.16.4 | MIT | Yes |
| six | 1.17.0 | MIT | Yes |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | Yes |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | No |
| sse-starlette | 3.5.0 | BSD-3-Clause | Yes |
| starlette | 1.8.0 | BSD-3-Clause | Yes |
| tenacity | 9.2.1 | Apache-2.0 | Yes |
| tiktoken | 0.14.0 | MIT License | Yes |
| tokenizers | 0.23.3 | Apache Software License | No |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | Yes |
| truststore | 0.10.5 | MIT | Yes |
| typing-inspection | 0.4.4 | MIT | Yes |
| typing_extensions | 4.16.0 | PSF-2.0 | Yes |
| urllib3 | 2.8.0 | MIT | Yes |
| uuid_utils | 0.17.1 | BSD-3-Clause | Yes |
| uvicorn | 0.54.0 | BSD-3-Clause | Yes |
| websockets | 16.1.1 | BSD-3-Clause | Yes |
| xxhash | 4.0.1 | BSD-2-Clause | Yes |
| yarl | 1.25.1 | Apache-2.0 | Yes |
| zipp | 4.1.1 | MIT | Yes |
| zstandard | 0.25.0 | BSD-3-Clause | Yes |
