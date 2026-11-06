"""数据源适配器的单元测试（本地目录 + 钉盘/飞书模拟清单，不访问网络）。"""

import json

from sources import DingTalkDriveSource, FeishuDriveSource, LocalFolderSource


def test_local_folder_reads_supported_files_only(tmp_path):
    (tmp_path / "说明.txt").write_text("滤网要晾干。", encoding="utf-8")
    (tmp_path / "忽略我.bin").write_bytes(b"\x00\x01")
    (tmp_path / "~$临时文件.docx").write_bytes(b"junk")

    items = list(LocalFolderSource(tmp_path, scope="X20 Pro").list_items())

    assert len(items) == 1
    assert items[0].filename == "说明.txt"
    assert items[0].source_key == "folder:说明.txt"
    assert items[0].scope == "X20 Pro"
    assert "滤网要晾干" in items[0].content


def test_local_folder_reports_unparsable_file(tmp_path):
    (tmp_path / "坏文件.pdf").write_bytes(b"%PDF-1.4 broken")

    items = list(LocalFolderSource(tmp_path).list_items())

    assert len(items) == 1
    assert items[0].error            # 解析失败要带原因，同步脚本会跳过并汇报
    assert items[0].content == ""


def test_dingtalk_source_follows_manifest(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "T30 故障.txt").write_text("T30 边刷异响先清理缠绕的毛发。", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "space_id": "mock-space-001",
        "files": [{
            "file_id": "dt-0001", "name": "T30 故障.txt", "path": "T30 故障.txt",
            "scope": "T30", "modified_time": "2026-10-14T10:00:00+08:00",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    items = list(DingTalkDriveSource(manifest).list_items())

    assert len(items) == 1
    assert items[0].source_key == "dingtalk:dt-0001"
    assert items[0].scope == "T30"
    assert "边刷异响" in items[0].content


def test_dingtalk_source_uses_generic_model_by_default(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "通用说明.txt").write_text("通用内容。", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "space_id": "mock-space-001",
        "files": [{"file_id": "dt-0009", "name": "通用说明.txt", "path": "通用说明.txt"}],
    }, ensure_ascii=False), encoding="utf-8")

    items = list(DingTalkDriveSource(manifest).list_items())

    assert items[0].scope == "通用"


def test_feishu_source_reads_files_and_cloud_docs(tmp_path):
    """飞书适配器要同时处理普通文件（下载解析）和云文档（raw_content）。"""
    files = tmp_path / "files"
    files.mkdir()
    (files / "办公用品.txt").write_text("在 OA 提交办公用品申领单。", encoding="utf-8")
    (files / "新人清单.txt").write_text("第 1 天办理入职手续。", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "folder_token": "mock-folder-001",
        "files": [
            {"token": "fldcn-file-1", "name": "办公用品.txt", "type": "file",
             "path": "办公用品.txt", "scope": "行政部", "visibility": "all"},
            {"token": "fldcn-doc-1", "name": "新人清单.txt", "type": "docx",
             "path": "新人清单.txt", "visibility": "staff"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    items = list(FeishuDriveSource(manifest).list_items())

    assert [item.source_key for item in items] == ["feishu:fldcn-file-1", "feishu:fldcn-doc-1"]
    assert items[0].scope == "行政部"
    assert "申领单" in items[0].content
    # 没写 scope 的云文档按通用资料处理，可见性跟随清单
    assert items[1].scope == "通用"
    assert items[1].visibility == "staff"
    assert "入职手续" in items[1].content
    assert items[1].extra["type"] == "docx"


def test_feishu_source_reports_missing_file(tmp_path):
    """清单里有、文件却不在时要带错误原因，同步脚本跳过并汇报，不能直接崩。"""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "folder_token": "mock-folder-001",
        "files": [{"token": "fldcn-file-9", "name": "缺失.txt", "type": "file",
                   "path": "缺失.txt"}],
    }, ensure_ascii=False), encoding="utf-8")

    items = list(FeishuDriveSource(manifest).list_items())

    assert len(items) == 1
    assert items[0].error
    assert items[0].content == ""
