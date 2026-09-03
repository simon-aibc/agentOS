import os
from pathlib import Path

from langchain_core.tools import tool

from agent_os.sandbox import get_read_root
from agent_os.schemas import GrepMatch, GrepResult


@tool
def grep(query: str, path: str = ".") -> GrepResult:
    """Search text files under a supplied project-relative path."""
    root = get_read_root()
    target_dir = (root / path).resolve()

    if not target_dir.is_relative_to(root):
        raise ValueError("Path is outside project root")

    matches = []

    if target_dir.is_file():
        if not target_dir.resolve().is_relative_to(root):
            raise ValueError("Path is outside project root")
        files_to_search = [target_dir]
    else:
        files_to_search = []
        for dirpath, _, filenames in os.walk(target_dir):
            dp = Path(dirpath)
            # basic filtering to avoid binary/git dirs
            if ".git" in dp.parts or "__pycache__" in dp.parts or ".venv" in dp.parts:
                continue
            for f in filenames:
                fpath = dp / f
                if fpath.resolve().is_relative_to(root):
                    files_to_search.append(fpath)

    for fpath in files_to_search:
        try:
            with open(fpath, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if query in line:
                        # try to get relative path from root, or just target_dir
                        rel_path = str(fpath.relative_to(root))
                        matches.append(
                            GrepMatch(path=rel_path, line=i, text=line.rstrip("\n"))
                        )
        except (UnicodeDecodeError, IsADirectoryError, PermissionError):
            pass

    return GrepResult(matches=matches)
