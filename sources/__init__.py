"""外部数据源适配器：把不同来源的资料统一成同一种待入库条目。"""

from sources.base import DataSource, SourceItem
from sources.dingtalk_drive import DingTalkDriveSource
from sources.dingtalk_files import (
    DingTalkFile, DingTalkFileProvider, MockDingTalkFileProvider,
    RealDingTalkFileProvider, get_dingtalk_file_provider,
)
from sources.feishu_docs import FeishuDriveSource
from sources.local_folder import LocalFolderSource

__all__ = ["DataSource", "SourceItem", "DingTalkDriveSource", "FeishuDriveSource",
           "LocalFolderSource", "DingTalkFile", "DingTalkFileProvider",
           "MockDingTalkFileProvider", "RealDingTalkFileProvider",
           "get_dingtalk_file_provider"]
