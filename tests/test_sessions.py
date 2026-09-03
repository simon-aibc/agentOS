from unittest.mock import AsyncMock, patch

import pytest

from agent_os.cli.app import async_main
from agent_os.sessions import get_session, list_sessions, upsert_session


@pytest.fixture
def mock_db(tmp_path):
    with patch(
        "agent_os.sessions._get_db_path", return_value=str(tmp_path / "test.db")
    ):
        yield tmp_path / "test.db"


def test_sessions_index_upsert(mock_db):
    # test upsert creating and updating
    upsert_session("th1", "Hello")
    sess = get_session("th1")
    assert sess["thread_id"] == "th1"
    assert sess["turn_count"] == 1
    assert sess["title"] == "Hello"

    upsert_session("th1", "New title")
    sess = get_session("th1")
    assert sess["turn_count"] == 2
    assert sess["title"] == "New title"


def test_sessions_list(mock_db):
    upsert_session("th1", "one")
    # wait to ensure different time? The isoformat uses microsecond precision so we don't need to sleep unless very fast
    import time

    time.sleep(0.01)
    upsert_session("th2", "two")

    lst = list_sessions()
    assert len(lst) == 2
    assert lst[0]["thread_id"] == "th2"  # latest first
    assert lst[1]["thread_id"] == "th1"


@pytest.mark.anyio
async def test_sessions_delete_confirmed(mock_db):
    upsert_session("th1", "one")
    assert get_session("th1") is not None

    with patch("agent_os.cli.app.EventFormatter"):
        with patch("agent_os.cli.app.input", return_value="y"):
            with patch(
                "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver.adelete_thread",
                new_callable=AsyncMock,
            ) as mock_del:
                await async_main(["sessions", "delete", "th1"], input_fn=lambda p: "y")
                mock_del.assert_called_once_with("th1")

    assert get_session("th1") is None


@pytest.mark.anyio
async def test_sessions_delete_rejected(mock_db):
    upsert_session("th2", "two")
    assert get_session("th2") is not None

    with patch("agent_os.cli.app.EventFormatter"):
        with patch("agent_os.cli.app.input", return_value="n"):
            with patch(
                "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver.adelete_thread",
                new_callable=AsyncMock,
            ) as mock_del:
                await async_main(["sessions", "delete", "th2"], input_fn=lambda p: "n")
                mock_del.assert_not_called()

    assert get_session("th2") is not None


@pytest.mark.anyio
async def test_sessions_inspect(mock_db):
    upsert_session("th3", "three")
    with patch("agent_os.cli.app.EventFormatter") as mock_fmt:
        await async_main(["sessions", "inspect", "th3"])
        mock_fmt.return_value.print_info.assert_called()
