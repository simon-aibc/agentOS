from agent_os.cli.formatter import EventFormatter


def _stream_content(content: object, formatter: EventFormatter) -> None:
    if isinstance(content, str):
        formatter.print_token(content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, str):
            formatter.print_token(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                formatter.print_token(text)


def _supervisor_destination(output: object) -> str | None:
    goto = getattr(output, "goto", None)
    if isinstance(goto, str):
        return goto
    if isinstance(goto, (list, tuple)) and goto:
        return str(goto[0])
    return None


def format_event(
    event: object,
    formatter: EventFormatter,
    *,
    verbose: bool = False,
) -> None:
    """Safely format one LangGraph v2 event without exposing raw state."""
    if not isinstance(event, dict):
        return

    event_type = event.get("event")
    name = event.get("name")
    data = event.get("data")
    metadata = event.get("metadata")
    if not isinstance(data, dict):
        data = {}
    if not isinstance(metadata, dict):
        metadata = {}

    if event_type == "on_chat_model_stream":
        chunk = data.get("chunk")
        _stream_content(getattr(chunk, "content", None), formatter)
        return

    if event_type == "on_tool_start" and isinstance(name, str) and name:
        formatter.print_tool_start(name, data.get("input"))
        return

    if event_type == "on_tool_end":
        output = data.get("output")
        formatter.print_tool_result(getattr(output, "content", output))
        return

    if event_type == "on_chain_end":
        node_name = metadata.get("langgraph_node")
        is_supervisor = name in {"supervisor", "supervisor_node"} or (
            node_name == "supervisor"
        )
        if is_supervisor:
            destination = _supervisor_destination(data.get("output"))
            if destination is not None:
                formatter.print_supervisor_route(destination)
        elif (
            verbose
            and isinstance(name, str)
            and name
            not in {
                "LangGraph",
                "__start__",
            }
        ):
            formatter.print_info(f"Node '{name}' completed.")
        return

    if (
        event_type == "on_chain_start"
        and verbose
        and isinstance(name, str)
        and name not in {"LangGraph", "__start__"}
    ):
        formatter.print_info(f"Node '{name}' started.")
