"""飞书云文档数据源（模拟实现）。

真实接入走飞书开放平台的三个接口，前提是企业管理员在开放平台创建自建应用并授权云文档权限：

    POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
         用 app_id / app_secret 换 tenant_access_token
    GET  https://open.feishu.cn/open-apis/drive/v1/files?folder_token=xxx
         列出文件夹下的文件
    GET  https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/download
         下载普通文件（Word / PDF / 图片）
    GET  https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content
         拉取飞书云文档（docx）的纯文本

和钉盘适配器一样，这里用「结构相同的本地清单 + 本地文件」替代网络调用：
manifest.json 的字段与飞书云文档接口对齐，文件内容放在 files/ 下。
接入生产时只改 _list_files() / _download() / _raw_content() 三个方法，
list_items() 的对外行为和后面的同步管道都不用动。
"""

import json
from pathlib import Path
from typing import Iterator

import documents
from database import GENERIC_MODEL, VISIBILITY_ALL, normalize_visibility
from sources.base import SourceItem


class FeishuDriveSource:
    """飞书云文档数据源。

    清单里每条记录对应一个文件：
        token        文件/文档标识（对应飞书的 file_token / document_id）
        name         文件名
        type         docx=飞书云文档（走 raw_content），其它按普通文件解析
        path        模拟环境里对应的本地文件路径
        scope        适用范围：型号 / 部门 / 业务线
        visibility   all=新人也能看，staff=仅员工可见
    """

    source_type = "feishu"

    def __init__(self, manifest_path: Path | str, files_dir: Path | str | None = None):
        self.manifest_path = Path(manifest_path)
        self.files_dir = Path(files_dir) if files_dir else self.manifest_path.parent / "files"

    def _load_manifest(self) -> dict:
        with self.manifest_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _list_files(self) -> list[dict]:
        """对应真实的「列出文件夹下的文件」接口。"""
        return self._load_manifest().get("files", [])

    def _download(self, record: dict) -> bytes:
        """对应真实的文件下载接口。"""
        return (self.files_dir / record["path"]).read_bytes()

    def _raw_content(self, record: dict) -> str:
        """对应飞书云文档的 raw_content 接口，返回纯文本。"""
        return (self.files_dir / record["path"]).read_text(encoding="utf-8")

    def list_items(self) -> Iterator[SourceItem]:
        manifest = self._load_manifest()
        folder_token = manifest.get("folder_token", "unknown")

        for record in self._list_files():
            source_key = f"feishu:{record['token']}"
            scope = record.get("scope") or GENERIC_MODEL
            visibility = normalize_visibility(record.get("visibility") or VISIBILITY_ALL)
            is_doc = (record.get("type") or "").lower() == "docx"
            try:
                text = (self._raw_content(record) if is_doc
                        else documents.extract_text(self._download(record), record["name"]))
            except (documents.UnsupportedFileType, documents.ExtractionFailed,
                    OSError, UnicodeDecodeError) as exc:
                yield SourceItem(source_key=source_key, filename=record["name"],
                                 title=record["name"], content="",
                                 source_type=self.source_type, scope=scope,
                                 visibility=visibility, error=f"{type(exc).__name__}: {exc}")
                continue

            yield SourceItem(
                source_key=source_key, filename=record["name"],
                title=Path(record["name"]).stem, content=text,
                source_type=self.source_type, scope=scope, visibility=visibility,
                extra={"folder_token": folder_token,
                       "type": record.get("type", "file"),
                       "edited_time": record.get("edited_time", "")},
            )
