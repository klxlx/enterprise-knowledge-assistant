"""会话存储与历史压缩的单元测试（写入临时目录，不碰真实聊天记录）。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import config_data as config
import file_history_store
import history_summary


@pytest.fixture(autouse=True)
def temporary_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    yield tmp_path


def add_turns(session_id: str, turns: int) -> None:
    history = file_history_store.get_history(session_id)
    messages = []
    for index in range(turns):
        messages.append(HumanMessage(content=f"第{index}个问题"))
        messages.append(AIMessage(content=f"第{index}个回答"))
    history.add_messages(messages)


def test_saved_session_shows_up_in_list():
    add_turns("user_1", 3)
    assert file_history_store.claim_session("user_1", "alice")

    sessions = file_history_store.list_sessions("alice")

    assert len(sessions) == 1
    assert sessions[0]["id"] == "user_1"
    assert sessions[0]["message_count"] == 6
    assert sessions[0]["turn_count"] == 3
    assert sessions[0]["preview"] == "第0个问题"
    assert sessions[0]["summarized"] is False
    # 别人和未登录用户都看不到
    assert file_history_store.list_sessions("bob") == []
    assert file_history_store.list_sessions() == []


def test_summary_directory_is_not_listed_as_session():
    add_turns("user_1", 1)
    assert file_history_store.claim_session("user_1", "alice")
    file_history_store.save_summary("user_1", "摘要内容", 2)

    sessions = file_history_store.list_sessions("alice")

    assert [item["id"] for item in sessions] == ["user_1"]
    assert sessions[0]["summarized"] is True


def test_delete_session_removes_messages_and_summary():
    add_turns("user_1", 1)
    assert file_history_store.claim_session("user_1", "alice")
    file_history_store.save_summary("user_1", "摘要内容", 2)

    assert file_history_store.delete_session("user_1") is True

    assert file_history_store.list_sessions("alice") == []
    assert file_history_store.load_summary("user_1") == {}
    assert file_history_store.load_owner("user_1") == ""
    assert file_history_store.delete_session("user_1") is False


def test_session_is_only_visible_to_owner():
    add_turns("user_1", 1)
    assert file_history_store.claim_session("user_1", "alice") is True

    assert file_history_store.can_access_session("user_1", "alice") is True
    assert file_history_store.can_access_session("user_1", "bob") is False
    assert file_history_store.can_access_session("user_1", "") is False
    assert [item["id"] for item in file_history_store.list_sessions("alice")] == ["user_1"]
    assert file_history_store.list_sessions("bob") == []


def test_unowned_session_is_only_open_to_anonymous():
    """历史遗留会话没有归属：登录用户访问不到，未登录用户仍能继续自己的匿名会话。"""
    add_turns("user_legacy", 1)

    assert file_history_store.can_access_session("user_legacy", "") is True
    assert file_history_store.can_access_session("user_legacy", "alice") is False
    assert file_history_store.list_sessions("alice") == []


def test_claim_session_is_first_writer_wins():
    add_turns("user_1", 1)

    assert file_history_store.claim_session("user_1", "alice") is True
    assert file_history_store.claim_session("user_1", "alice") is True
    assert file_history_store.claim_session("user_1", "bob") is False
    assert file_history_store.load_owner("user_1") == "alice"


def test_invalid_session_id_is_rejected():
    for bad in ("../etc/passwd", "a/b", "名字", ""):
        assert file_history_store.is_valid_session_id(bad) is False
    assert file_history_store.is_valid_session_id("user_1791813136431") is True


def test_prepare_history_keeps_recent_turns_and_summarizes_older(monkeypatch):
    monkeypatch.setattr(config, "history_keep_turns", 2)
    monkeypatch.setattr(config, "history_summary_after_messages", 4)
    monkeypatch.setattr(config, "history_summary_trigger_messages", 2)

    calls = []

    def fake_summarize(previous, messages):
        calls.append((previous, len(messages)))
        return f"摘要-{len(calls)}"

    monkeypatch.setattr(history_summary, "summarize", fake_summarize)

    messages = []
    for index in range(6):
        messages.append(HumanMessage(content=f"问题{index}"))
        messages.append(AIMessage(content=f"回答{index}"))

    summary, recent = history_summary.prepare_history("user_1", messages)

    assert summary == "摘要-1"
    assert len(recent) == 4                     # 只保留最近 2 轮
    assert calls == [("", 8)]                   # 12 条里前 8 条被压缩
    assert file_history_store.load_summary("user_1")["covered_messages"] == 8

    # 再来一轮：只把新攒下的部分并进已有摘要
    messages += [HumanMessage(content="新问题"), AIMessage(content="新回答")]
    summary, recent = history_summary.prepare_history("user_1", messages)

    assert summary == "摘要-2"
    assert calls[-1] == ("摘要-1", 2)
    assert len(recent) == 4


def test_short_history_is_returned_unchanged():
    messages = [HumanMessage(content="问题"), AIMessage(content="回答")]

    summary, recent = history_summary.prepare_history("user_1", messages)

    assert summary == ""
    assert recent == messages
