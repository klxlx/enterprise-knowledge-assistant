"""把没有归属的历史会话分配给某个账号。

会话改成按账号隔离后，登录用户只能看到自己的会话。早期（登录入口不明显时）
产生的会话没有归属记录，默认对登录用户不可见。这个脚本用来做一次性的认领：

    python scripts/assign_sessions.py --list-unowned
    python scripts/assign_sessions.py --username staff --all-unowned
    python scripts/assign_sessions.py --username newbie --session user_abc_123

认领是原子的：已经被别人认领的会话不会被改写，只会跳过并提示。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse

import file_history_store


def unowned_sessions() -> list[str]:
    directory = file_history_store.history_dir()
    if not directory.is_dir():
        return []
    sessions = []
    for path in directory.iterdir():
        if not path.is_file() or not file_history_store.is_valid_session_id(path.name):
            continue
        if not file_history_store.load_owner(path.name):
            sessions.append(path.name)
    return sorted(sessions)


def main() -> int:
    parser = argparse.ArgumentParser(description="认领没有归属的历史会话")
    parser.add_argument("--username", help="分配给哪个账号（必填，除非只查看）")
    parser.add_argument("--session", action="append", default=[],
                        help="指定会话 ID，可以重复写多次")
    parser.add_argument("--all-unowned", action="store_true",
                        help="把当前所有没有归属的会话都分配给他")
    parser.add_argument("--list-unowned", action="store_true",
                        help="只看哪些会话还没有归属")
    args = parser.parse_args()

    pending = unowned_sessions()
    if args.list_unowned or not args.username:
        print(f"没有归属的会话（{len(pending)}）：")
        for session_id in pending:
            print(f"  {session_id}")
        if not args.username:
            return 0

    targets = args.session or (pending if args.all_unowned else [])
    if not targets:
        raise SystemExit("请用 --session 指定会话，或者加 --all-unowned")

    assigned, skipped = [], []
    for session_id in targets:
        if not file_history_store.is_valid_session_id(session_id):
            skipped.append((session_id, "会话 ID 不合法"))
            continue
        if file_history_store.claim_session(session_id, args.username):
            assigned.append(session_id)
        else:
            owner = file_history_store.load_owner(session_id)
            skipped.append((session_id, f"已属于 {owner or '未知账号'}"))

    print(f"已分配给 {args.username}：{len(assigned)} 个")
    for session_id in assigned:
        print(f"  ✓ {session_id}")
    if skipped:
        print(f"跳过：{len(skipped)} 个")
        for session_id, reason in skipped:
            print(f"  - {session_id}：{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
