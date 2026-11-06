"""钉钉云盘数据源（模拟实现）。

真实接入要调用钉钉开放平台的钉盘接口，前提是企业在钉钉开放平台创建内部应用并授权：

    POST https://api.dingtalk.com/v1.0/drive/spaces/{spaceId}/files/list     列出文件
    POST https://api.dingtalk.com/v1.0/drive/spaces/{spaceId}/files/{id}/download  下载文件

申请企业授权不是个人开发者能自助完成的，所以这里用「结构相同的本地清单 + 本地文件」
替代这两个接口：manifest.json 的字段与钉盘返回保持一致，文件内容放在 files/ 下。
接入生产环境时，只需要把 _load_manifest() 和 _download() 换成真实的 HTTP 调用，
list_items() 的对外行为和同步管道都不用改。
"""

import json
from pathlib import Path
from typing import Iterator

import documents
from database import GENERIC_MODEL, VISIBILITY_ALL, normalize_visibility
from sources.base import SourceItem


class DingTalkDriveSource:
    source_type = "dingtalk"

    def __init__(self, manifest_path: Path | str, files_dir: Path | str | None = None):
        self.manifest_path = Path(manifest_path)
        self.files_dir = Path(files_dir) if files_dir else self.manifest_path.parent / "files"

    def _load_manifest(self) -> dict:
        with self.manifest_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _download(self, record: dict) -> bytes:
        """对应真实的文件下载接口。"""
        return (self.files_dir / record["path"]).read_bytes()

    def list_items(self) -> Iterator[SourceItem]:
        manifest = self._load_manifest()
        space_id = manifest.get("space_id", "unknown")

        for record in manifest.get("files", []):
            source_key = f"dingtalk:{record['file_id']}"
            model = record.get("scope") or GENERIC_MODEL
            visibility = normalize_visibility(record.get("visibility") or VISIBILITY_ALL)
            try:
                text = documents.extract_text(self._download(record), record["name"])
            except (documents.UnsupportedFileType, documents.ExtractionFailed) as exc:
                yield SourceItem(source_key=source_key, filename=record["name"],
                                 title=record["name"], content="",
                                 source_type=self.source_type, scope=model,
                                 visibility=visibility,
                                 error=str(exc))
                continue

            yield SourceItem(
                source_key=source_key, filename=record["name"],
                title=Path(record["name"]).stem, content=text,
                source_type=self.source_type, scope=model, visibility=visibility,
                extra={"space_id": space_id, "modified_time": record.get("modified_time", "")},
            )
