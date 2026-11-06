"""审计日志查看。

用法：
    python scripts/view_audit.py                        最近 30 条
    python scripts/view_audit.py --limit 100            看更多
    python scripts/view_audit.py --action knowledge.upload
    python scripts/view_audit.py --username staff
    python scripts/view_audit.py --stats                按动作统计条数

记哪些动作：
    auth.login / auth.logout        登录、退出（含登录失败）
    knowledge.upload                上传资料（带适用范围、可见性、时效）
    session.delete                  删除会话
    feedback.submit                 提交反馈
    handoff.created                 转人工建单
    employee.create                 新增员工卡片
    employee.delete                 删除员工卡片
    dingtalk.download               下载资料文件
    file.import                     把资料文件导入知识库
审计写入是旁路：写日志失败只打警告，不影响业务请求。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
from collections import Counter

import database


def print_stats(limit: int) -> None:
    logs = database.list_audit_logs(limit=limit)
    counter = Counter(entry["action"] for entry in logs)
    failed = sum(1 for entry in logs if entry["status"] != "ok")
    print(f"最近 {len(logs)} 条审计日志（失败 {failed} 条）")
    print("-" * 78)
    for action, count in counter.most_common():
        print(f"  {action:<24}{count:>5}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看审计日志")
    parser.add_argument("--limit", type=int, default=30, help="最多显示多少条")
    parser.add_argument("--action", help="只显示某个动作，例如 knowledge.upload")
    parser.add_argument("--username", help="只显示某个账号的操作")
    parser.add_argument("--stats", action="store_true", help="按动作统计条数")
    args = parser.parse_args()

    if args.stats:
        print_stats(max(args.limit, 200))
        return 0

    logs = database.list_audit_logs(limit=args.limit, action=args.action,
                                    username=args.username)
    print(f"审计日志（{len(logs)} 条）")
    print("-" * 78)
    if not logs:
        print("  没有匹配的记录。")
        return 0
    for entry in logs:
        who = entry["username"] or "未登录"
        role = f"/{entry['role']}" if entry["role"] else ""
        flag = "" if entry["status"] == "ok" else f"  [{entry['status']}]"
        target = f"  → {entry['target']}" if entry["target"] else ""
        print(f"{entry['created_at'][:19]}  {entry['action']:<20} {who}{role}{target}{flag}")
        if entry["detail"]:
            detail = "，".join(f"{key}={value}" for key, value in entry["detail"].items())
            print(f"        {detail[:88]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
