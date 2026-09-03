"""Isolated deterministic preflight for one human-authored skill package."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_os.skill_authoring import _assert_fresh_loader


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print(
            "usage: skill_authoring_preflight PACKAGE_DIR EXPECTED_SKILL_NAME",
            file=sys.stderr,
        )
        return 2
    try:
        _assert_fresh_loader(Path(arguments[0]).resolve(), arguments[1])
    except Exception as exc:
        print(f"preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
