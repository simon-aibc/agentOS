from langchain_core.tools import tool

from agent_os.schemas import ArchitectBrief


@tool
def plan_writer(
    files: list[str], changes: list[str], verify_cmd: str
) -> ArchitectBrief:
    """Write an ArchitectBrief to memory without side effects."""
    return ArchitectBrief(files=files, changes=changes, verify_cmd=verify_cmd)
