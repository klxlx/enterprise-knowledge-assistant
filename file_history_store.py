"""基于文件的会话历史存储。

每个会话一个 JSON 文件，文件名就是会话 ID。较早的对话会被压缩成摘要，
存放在 chat_history/summaries/<会话 ID>.json，避免每次请求都把整段历史发给模型。
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict

import config_data as config

# 会话 ID 会成为文件名，只允许字母、数字、下划线和连字符。
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
SUMMARY_DIR_NAME = "summaries"
SCOPE_DIR_NAME = "scopes"
OWNER_DIR_NAME = "owners"
PREVIEW_CHARS = 60


def history_dir() -> Path:
    return Path(config.DATA_ROOT) / "chat_history"


def summary_dir() -> Path:
    return history_dir() / SUMMARY_DIR_NAME


def scope_dir() -> Path:
    return history_dir() / SCOPE_DIR_NAME


def owner_dir() -> Path:
    return history_dir() / OWNER_DIR_NAME


def owner_path(session_id: str) -> Path:
    return owner_dir() / f"{session_id}.json"


def load_owner(session_id: str) -> str:
    """会话归属的账号；没有归属文件的老会话返回空字符串。"""
    if not is_valid_session_id(session_id):
        return ""
    payload = _read_json(owner_path(session_id)) or {}
    return str(payload.get("username", ""))


def claim_session(session_id: str, username: str) -> bool:
    """把会话认领给某个账号。

    用 O_EXCL 原子创建归属文件：两个人同时认领同一个会话时只有一个成功。
    已经属于别人就返回 False，调用方应当拒绝访问。
    """
    if not username or not is_valid_session_id(session_id):
        return False
    path = owner_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return load_owner(session_id) == username
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump({"username": username, "created_at": datetime.now(timezone.utc).isoformat()},
                  stream, ensure_ascii=False)
    return True


def can_access_session(session_id: str, username: str = "") -> bool:
    """有没有权限读/删这个会话。

    - 有归属：必须是本人；
    - 没有归属（历史遗留或匿名会话）：只允许未登录访问。
    """
    owner = load_owner(session_id)
    if owner:
        return owner == username
    return not username


def is_valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id or ""))


def get_history(session_id):
    return FileChatMessageHistory(session_id, str(history_dir()))


class FileChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, session_id: str, storage_path: str):
        self.session_id = session_id
        self.storage_path = storage_path
        self.file_path = os.path.join(self.storage_path, self.session_id)
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        # 获取已有消息并合并新消息
        all_messages = list(self.messages)
        all_messages.extend(messages)
        # 将消息转为字典并写入文件
        new_messages = [message_to_dict(message) for message in all_messages]
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(new_messages, f, ensure_ascii=False)

    @property
    def messages(self) -> List[BaseMessage]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                messages_data = json.load(f)
            return messages_from_dict(messages_data)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def clear(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([], f)


def load_messages(session_id: str) -> List[BaseMessage]:
    return get_history(session_id).messages


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_sessions(username: str = "") -> List[dict]:
    """列出某个账号自己的会话，最近的排前面；未登录不返回任何历史。"""
    if not username:
        return []
    directory = history_dir()
    if not directory.is_dir():
        return []

    sessions = []
    for path in directory.iterdir():
        if not path.is_file() or not is_valid_session_id(path.name):
            continue
        raw = _read_json(path)
        if not raw:
            continue
        if load_owner(path.name) != username:
            continue
        preview = next(
            (item.get("data", {}).get("content", "") for item in raw
             if item.get("type") == "human"),
            "",
        )
        summary = load_summary(path.name).get("summary", "")
        sessions.append({
            "id": path.name,
            "message_count": len(raw),
            "turn_count": sum(1 for item in raw if item.get("type") == "human"),
            "updated_at": datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat(),
            "preview": preview.replace("\n", " ")[:PREVIEW_CHARS],
            "summarized": bool(summary),
        })
    sessions.sort(key=lambda item: item["updated_at"], reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """删除会话文件、摘要、范围和归属记录，返回是否真的删掉了东西。"""
    if not is_valid_session_id(session_id):
        return False
    removed = False
    for path in (history_dir() / session_id, summary_path(session_id),
                 scope_path(session_id), owner_path(session_id)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
    return removed


def summary_path(session_id: str) -> Path:
    return summary_dir() / f"{session_id}.json"


def load_summary(session_id: str) -> dict:
    if not is_valid_session_id(session_id):
        return {}
    return _read_json(summary_path(session_id)) or {}


def save_summary(session_id: str, summary: str, covered_messages: int) -> None:
    directory = summary_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "covered_messages": covered_messages,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path(session_id).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def scope_path(session_id: str) -> Path:
    return scope_dir() / f"{session_id}.txt"


def load_session_scope(session_id: str) -> str:
    """读取本会话记住的「适用范围」；用户提过一次，后面就不用重复说。"""
    if not is_valid_session_id(session_id):
        return ""
    try:
        return scope_path(session_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_session_scope(session_id: str, scope: str) -> None:
    if not is_valid_session_id(session_id) or not scope:
        return
    scope_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    scope_path(session_id).write_text(scope, encoding="utf-8")
