"""钉钉文件检索：可替换的 provider。

对话里说「帮我在钉盘上找中秋后战略安排」或「张伟在群里发的资料」时，
模型调用 search_dingtalk_files，provider 负责把关键词/发布人/日期
变成文件列表；界面再提供下载按钮。

现在默认走 MockDingTalkFileProvider：读 mock/dingtalk/index.json 模拟钉盘索引，
文件内容放在 mock/dingtalk/files/。拿到企业应用授权后，把 DINGTALK_MODE 设成 real，
在 RealDingTalkFileProvider 里补两个方法即可：

    _search_files()   调用钉盘/群文件的搜索接口，返回原始记录列表
    _download_file()  按 file_id 下载文件内容

上层（工具、接口、前端、评测）都不需要改。
"""

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import config_data as config
from database import normalize_document_visibility

SEARCH_LIMIT = 8
MIN_SCORE = 4

# 口语里的"帮我找一下…资料"这些词不参与匹配，否则每个文件都能命中
STOP_PHRASES = (
    "帮我在", "帮我", "麻烦", "在钉盘上", "钉盘上", "在钉钉上", "钉钉上",
    "在群里", "群里面", "群内", "聊天记录", "聊天", "发送的", "发的",
    "搜索", "搜一下", "找一下", "找找", "查找", "查一下", "关于",
    "相关的", "相关资料", "资料", "文件", "一下", "都有哪些", "有哪些",
)
SPLIT_PATTERN = re.compile(r"[\s，。！？、,.!?;；:：]+")


@dataclass(frozen=True)
class DingTalkFile:
    """一份钉钉文件。字段和真实接口返回保持一一对应。"""

    file_id: str
    name: str
    publisher: str
    group: str
    published_at: str
    description: str = ""
    keywords: str = ""
    department: str = "通用"
    visibility: str = "all"
    size: int = 0
    path: str = ""

    def as_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "publisher": self.publisher,
            "group": self.group,
            "published_at": self.published_at,
            "description": self.description,
            "department": self.department,
            "visibility": self.visibility,
            "size": self.size,
        }


class DingTalkFileProvider(Protocol):
    """文件检索/下载的最小约定。"""

    def search(self, query: str, publisher: str = "", date_from: str = "",
               date_to: str = "") -> list[DingTalkFile]:
        ...

    def list_all(self) -> list[DingTalkFile]:
        ...

    def get(self, file_id: str) -> DingTalkFile | None:
        ...

    def read(self, file_id: str) -> tuple[bytes, str]:
        """返回（文件内容, 文件名）。"""
        ...


def _normalize(text: str) -> str:
    return "".join(str(text or "").lower().split())


def query_chunks(query: str) -> list[str]:
    """把口语问题拆成真正的检索词：去掉"帮我/钉盘/资料"后按标点切段。"""
    cleaned = query or ""
    for phrase in STOP_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    chunks = []
    for piece in SPLIT_PATTERN.split(cleaned):
        chunk = _normalize(piece)
        if len(chunk) >= 2:
            chunks.append(chunk)
    return chunks


def query_terms(query: str) -> list[str]:
    """在检索词上生成 2~6 字片段，长片段权重更高。"""
    terms: set[str] = set()
    for chunk in query_chunks(query):
        # 2~6 字片段：长片段权重更高，能压住"部门"这种通用词的噪声
        for size in range(2, min(6, len(chunk)) + 1):
            for start in range(0, len(chunk) - size + 1):
                terms.add(chunk[start:start + size])
    return sorted(terms, key=len, reverse=True)


def score_file(item: DingTalkFile, terms: list[str]) -> tuple[int, int]:
    """返回（得分, 最长命中片段长度）。"""
    haystack = _normalize(" ".join(
        [item.name, item.description, item.keywords.replace("|", " "), item.publisher]
    ))
    matched = [term for term in terms if term in haystack]
    return sum(len(term) ** 2 for term in matched), max(
        (len(term) for term in matched), default=0)


class MockDingTalkFileProvider:
    """本地索引模拟钉盘：字段与真实接口一致，搜到的文件可以真的下载。"""

    def __init__(self, index_path: Path | str | None = None,
                 files_dir: Path | str | None = None):
        self.index_path = Path(index_path or config.dingtalk_index_path)
        self.files_dir = Path(files_dir) if files_dir else self.index_path.parent / "files"
        self._files: list[DingTalkFile] = self._load()

    def _load(self) -> list[DingTalkFile]:
        with self.index_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        files = []
        for record in payload.get("files", []):
            files.append(DingTalkFile(
                file_id=record["file_id"],
                name=record["name"],
                publisher=record.get("publisher", ""),
                group=record.get("group", ""),
                published_at=record.get("published_at", ""),
                description=record.get("description", ""),
                keywords=record.get("keywords", ""),
                department=record.get("department", "通用"),
                visibility=normalize_document_visibility(record.get("visibility")),
                size=int(record.get("size") or 0),
                path=record.get("path", record["name"]),
            ))
        return files

    def search(self, query: str, publisher: str = "", date_from: str = "",
               date_to: str = "") -> list[DingTalkFile]:
        start = _parse_date(date_from, "date_from")
        end = _parse_date(date_to, "date_to")
        if start and end and end < start:
            raise ValueError("结束日期不能早于开始日期")

        terms = query_terms(query)
        chunks = query_chunks(query)
        # 至少要命中检索词里的 min(3, 词长) 个字，避免「说明」这种通用词把无关文件带出来
        required = min(3, max((len(chunk) for chunk in chunks), default=0))
        wanted_publisher = _normalize(publisher)
        results: list[tuple[int, DingTalkFile]] = []
        for item in self._files:
            if wanted_publisher and wanted_publisher not in _normalize(
                    item.publisher + item.file_id):
                continue
            published = _parse_date(item.published_at, "published_at")
            if start and published and published < start:
                continue
            if end and published and published > end:
                continue
            if terms:
                score, longest = score_file(item, terms)
                if score < MIN_SCORE or longest < required:
                    continue
            else:
                score = 1
            results.append((score, item))

        results.sort(key=lambda pair: (pair[0], pair[1].published_at), reverse=True)
        return [item for _, item in results[:SEARCH_LIMIT]]

    def get(self, file_id: str) -> DingTalkFile | None:
        return next((item for item in self._files if item.file_id == file_id), None)

    def list_all(self) -> list[DingTalkFile]:
        return sorted(self._files, key=lambda item: item.published_at, reverse=True)

    def read(self, file_id: str) -> tuple[bytes, str]:
        item = self.get(file_id)
        if item is None:
            raise FileNotFoundError(f"没有找到文件 {file_id}")
        return (self.files_dir / item.path).read_bytes(), item.name


def _parse_date(value: str, field: str) -> date | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field} 需要 YYYY-MM-DD 格式") from exc


class RealDingTalkFileProvider:
    """真实钉钉接入骨架。

    已经把"拿 token"和"上层接口"写好，拿到企业应用授权后只需要补：
      - _search_files()：调用钉盘的搜索/文件列表接口，返回原始记录
      - _download_file()：按 file_id 下载文件
    原始记录的字段映射在 _to_file() 里，按接口实际字段名调整即可。
    """

    TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"

    def __init__(self, app_key: str, app_secret: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self._token = ""
        self._token_expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        if not self.app_key or not self.app_secret:
            raise RuntimeError("DINGTALK_APP_KEY / DINGTALK_APP_SECRET 未配置")
        payload = json.dumps({
            "appKey": self.app_key, "appSecret": self.app_secret,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.TOKEN_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        token = result.get("accessToken", "")
        if not token:
            raise RuntimeError(f"获取钉钉 accessToken 失败：{result}")
        self._token = token
        self._token_expires_at = time.time() + int(result.get("expireIn", 7200))
        return token

    def _search_files(self, token: str, query: str, publisher: str,
                      date_from: str, date_to: str) -> list[dict]:
        # TODO(拿到企业授权后)：调用钉盘/群文件搜索接口，返回原始记录列表。
        # 建议把 query/publisher/date 作为服务端过滤条件；接口不支持时先拉一页回来再过滤。
        raise NotImplementedError(
            "真实钉钉检索还没接：拿到企业应用授权后，在这里调用钉盘文件搜索接口。"
        )

    def _download_file(self, token: str, file_id: str) -> tuple[bytes, str]:
        # TODO(拿到企业授权后)：按 file_id 调下载接口，返回（文件内容, 文件名）。
        raise NotImplementedError(
            "真实钉钉下载还没接：拿到企业应用授权后，在这里调用钉盘下载接口。"
        )

    @staticmethod
    def _to_file(record: dict) -> DingTalkFile:
        """把钉钉接口的原始字段映射成内部结构（字段名按实际接口调整）。"""
        return DingTalkFile(
            file_id=str(record.get("fileId") or record.get("file_id") or ""),
            name=str(record.get("name") or record.get("fileName") or ""),
            publisher=str(record.get("publisher") or record.get("creatorName") or ""),
            group=str(record.get("group") or record.get("spaceName") or ""),
            published_at=str(record.get("publishedAt") or record.get("modifiedTime") or ""),
            description=str(record.get("description") or ""),
            keywords=str(record.get("keywords") or ""),
            department=str(record.get("department") or "通用"),
            visibility=normalize_document_visibility(record.get("visibility")),
            size=int(record.get("size") or 0),
        )

    def search(self, query: str, publisher: str = "", date_from: str = "",
               date_to: str = "") -> list[DingTalkFile]:
        token = self._access_token()
        records = self._search_files(token, query, publisher, date_from, date_to)
        return [self._to_file(record) for record in records][:SEARCH_LIMIT]

    def get(self, file_id: str) -> DingTalkFile | None:
        # 真实接口没有"按 id 查详情"的通用方法；下载时直接用 file_id，
        # 界面展示的信息由 search 返回。这里返回占位对象，供下载接口取名字。
        return DingTalkFile(file_id=file_id, name=file_id, publisher="", group="",
                            published_at="")

    def list_all(self) -> list[DingTalkFile]:
        return self.search("")

    def read(self, file_id: str) -> tuple[bytes, str]:
        token = self._access_token()
        return self._download_file(token, file_id)


@lru_cache(maxsize=1)
def get_dingtalk_file_provider() -> DingTalkFileProvider:
    if config.dingtalk_mode == "real":
        return RealDingTalkFileProvider(config.dingtalk_app_key, config.dingtalk_app_secret)
    return MockDingTalkFileProvider(config.dingtalk_index_path)
