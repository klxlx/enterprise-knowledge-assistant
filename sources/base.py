"""数据源适配器的公共约定。

新增一个来源（钉盘、飞书、Confluence…）只需要实现 list_items()，
后面的解析、分块、向量化、入库完全复用同一条管道。
"""

from dataclasses import dataclass, field
from typing import Iterator, Protocol

from database import GENERIC_MODEL, VISIBILITY_ALL


@dataclass(frozen=True)
class SourceItem:
    """一个待入库的文档。"""

    source_key: str          # 来源内的唯一标识，例如 folder:手册/a.txt、dingtalk:dt-001
    filename: str            # 展示用的原始文件名
    title: str               # 标题，用作分块前缀
    content: str             # 纯文本内容
    source_type: str         # folder / dingtalk / upload
    scope: str = GENERIC_MODEL          # 适用范围：型号 / 部门 / 业务线
    visibility: str = VISIBILITY_ALL    # all=新人也能看，staff=仅员工可见
    error: str = ""          # 解析失败时填原因，同步时跳过并汇报
    extra: dict = field(default_factory=dict)


class DataSource(Protocol):
    """数据源接口：给同步脚本用的最小约定。"""

    source_type: str

    def list_items(self) -> Iterator[SourceItem]:
        """列出当前所有可入库的文档。"""
        ...
