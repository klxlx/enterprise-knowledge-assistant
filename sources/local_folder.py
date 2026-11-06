"""本地目录 / 共享盘数据源。

把资料丢进指定目录（例如 \\fileserver\新人资料），同步脚本就能把它收进知识库。
"""

from pathlib import Path
from typing import Iterator

import documents
from database import GENERIC_MODEL, VISIBILITY_ALL, normalize_visibility
from sources.base import SourceItem


class LocalFolderSource:
    source_type = "folder"

    def __init__(self, root: Path | str, scope: str = GENERIC_MODEL,
                 recursive: bool = True, visibility: str = VISIBILITY_ALL):
        self.root = Path(root)
        self.scope = scope
        self.recursive = recursive
        self.visibility = normalize_visibility(visibility)

    def list_items(self) -> Iterator[SourceItem]:
        if not self.root.is_dir():
            raise FileNotFoundError(f"目录不存在：{self.root}")

        pattern = "**/*" if self.recursive else "*"
        for path in sorted(self.root.glob(pattern)):
            if not path.is_file() or path.suffix.lower() not in documents.SUPPORTED_SUFFIXES:
                continue
            if path.name.startswith("~$"):        # Office 打开文件时的临时文件
                continue

            source_key = f"folder:{path.relative_to(self.root).as_posix()}"
            try:
                text = documents.extract_text(path.read_bytes(), path.name)
            except (documents.UnsupportedFileType, documents.ExtractionFailed) as exc:
                yield SourceItem(source_key=source_key, filename=path.name,
                                 title=path.stem, content="",
                                 source_type=self.source_type, visibility=self.visibility,
                                 scope=self.scope, error=str(exc))
                continue

            yield SourceItem(
                source_key=source_key, filename=path.name, title=path.stem, content=text,
                source_type=self.source_type, scope=self.scope,
                visibility=self.visibility,
                extra={"path": str(path)},
            )
