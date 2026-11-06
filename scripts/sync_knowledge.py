"""把外部数据源同步进知识库。

用法：
    python scripts/sync_knowledge.py --source folder --path "D:\\新人资料" --scope "人力资源部"
    python scripts/sync_knowledge.py --source dingtalk --manifest mock/dingtalk/manifest.json
    python scripts/sync_knowledge.py --source feishu --manifest mock/feishu/manifest.json
    python scripts/sync_knowledge.py --source dingtalk --manifest ... --webhook "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    python scripts/sync_knowledge.py --source folder --path ... --dry-run
    python scripts/sync_knowledge.py --source folder --path "D:\\2026 制度" --expires-at 2026-12-31

同步规则：
    新增  来源里有、库里没有            → 入库
    更新  同一来源内容变了              → 旧版本下线，新版本入库
    跳过  内容没变                      → 不做任何事
    下线  来源里已经删掉                → 标记为历史版本（软删除，历史仍保留）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import hashlib
from datetime import date

import platform_setup  # noqa: F401  必须先于 vector_stores / knowledge_base 导入
from database import (
    GENERIC_MODEL, VISIBILITY_ALL, VISIBILITY_STAFF,
    list_source_documents, retire_document,
)
from knowledge_base import KnowledgeBaseService
from sources import DingTalkDriveSource, FeishuDriveSource, LocalFolderSource
from sources.notify import send_dingtalk_text


def parse_date(value: str | None, field: str) -> str | None:
    """命令行日期参数校验，格式不对直接退出并说清原因。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise SystemExit(f"{field} 需要 YYYY-MM-DD 格式的日期：{value}") from exc


def build_source(args):
    if args.source == "folder":
        if not args.path:
            raise SystemExit("目录同步必须指定 --path")
        return LocalFolderSource(args.path, scope=args.scope, visibility=args.visibility)
    if args.source == "dingtalk":
        if not args.manifest:
            raise SystemExit("钉盘同步必须指定 --manifest")
        return DingTalkDriveSource(args.manifest)
    if args.source == "feishu":
        if not args.manifest:
            raise SystemExit("飞书同步必须指定 --manifest")
        return FeishuDriveSource(args.manifest)
    raise SystemExit(f"未知数据源：{args.source}")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步外部数据源到知识库")
    parser.add_argument("--source", choices=["folder", "dingtalk", "feishu"],
                        required=True)
    parser.add_argument("--path", help="目录同步时的根目录")
    parser.add_argument("--manifest", help="钉盘模拟同步时的清单文件")
    parser.add_argument("--scope", default=GENERIC_MODEL,
                        help="目录同步时统一使用的适用范围（型号/部门/业务线）")
    parser.add_argument("--visibility", default=VISIBILITY_ALL,
                        choices=[VISIBILITY_ALL, VISIBILITY_STAFF],
                        help="可见性：all=新人也能看，staff=仅员工可见")
    parser.add_argument("--dry-run", action="store_true", help="只显示会做什么，不写库")
    parser.add_argument("--webhook", help="钉钉群机器人 webhook，同步结果推送到群里")
    parser.add_argument("--effective-from", metavar="YYYY-MM-DD",
                        help="同步进来的资料统一生效日期（默认今天）")
    parser.add_argument("--expires-at", metavar="YYYY-MM-DD",
                        help="同步进来的资料统一失效日期（默认长期有效）")
    args = parser.parse_args()

    effective_from = parse_date(args.effective_from, "--effective-from")
    expires_at = parse_date(args.expires_at, "--expires-at")
    if effective_from and expires_at and expires_at < effective_from:
        raise SystemExit("失效日期不能早于生效日期")

    source = build_source(args)
    existing = {doc["source_key"]: doc for doc in list_source_documents(source.source_type)}

    added, updated, skipped, retired, failed = [], [], [], [], []
    seen_keys = set()
    service = None if args.dry_run else KnowledgeBaseService()

    for item in source.list_items():
        seen_keys.add(item.source_key)
        if item.error:
            failed.append(f"{item.filename}（{item.error}）")
            continue

        digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        current = existing.get(item.source_key)
        if current and current["document_hash"] == digest:
            skipped.append(item.filename)
            continue

        if args.dry_run:
            (updated if current else added).append(item.filename)
            continue

        if current:
            # 内容变了：旧版本先下线，保证检索不会同时命中新旧两份
            retire_document(current["document_hash"])

        message = service.upload(
            item.content, item.filename, item.scope, item.visibility,
            source_type=item.source_type, source_key=item.source_key,
            effective_from=effective_from, expires_at=expires_at,
        )
        if message.startswith("[成功]"):
            (updated if current else added).append(item.filename)
        else:
            skipped.append(f"{item.filename}（{message}）")

    for source_key, doc in existing.items():
        if source_key in seen_keys:
            continue
        if not args.dry_run:
            retire_document(doc["document_hash"])
        retired.append(doc["filename"])

    prefix = "[预演] " if args.dry_run else ""
    summary = (
        f"{prefix}知识库同步完成（来源：{args.source}）："
        f"新增 {len(added)}、更新 {len(updated)}、跳过 {len(skipped)}、"
        f"下线 {len(retired)}、失败 {len(failed)}"
    )
    print(summary)
    for label, entries in (("新增", added), ("更新", updated),
                           ("跳过", skipped), ("下线", retired), ("失败", failed)):
        for entry in entries:
            print(f"  {label}：{entry}")

    if args.webhook:
        ok, detail = send_dingtalk_text(args.webhook, summary)
        print("钉钉通知：已发送" if ok else f"钉钉通知未发送（{detail}）")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
