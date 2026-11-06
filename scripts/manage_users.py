"""账号管理。

用法：
    python scripts/manage_users.py --list
    python scripts/manage_users.py --create alice --name "王工" --role staff --department "客户服务部" --password "xxx"
    python scripts/manage_users.py --seed-demo        # 建三个演示账号（客服主管、人事主管、新人）

角色（内部值）：
  staff    = 部门主管（可导入本部门资料、看盲区/审计）
  newcomer = 新人（可导入本部门或通用资料、提问、看通讯录）
人事部门的主管（department=人力资源部）可以跨部门管理和查看敏感资料。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse

import auth
import database
from database import ROLE_NEWCOMER, ROLE_STAFF

DEMO_ACCOUNTS = (
    ("staff", "王工", ROLE_STAFF, "客户服务部", "staff123"),
    ("hr", "李敏", ROLE_STAFF, "人力资源部", "hr123"),
    ("newbie", "小李", ROLE_NEWCOMER, "客户服务部", "newbie123"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="账号管理")
    parser.add_argument("--list", action="store_true", help="列出所有账号")
    parser.add_argument("--create", metavar="用户名", help="新建或更新账号")
    parser.add_argument("--name", help="显示名")
    parser.add_argument("--role", choices=[ROLE_STAFF, ROLE_NEWCOMER], help="角色")
    parser.add_argument("--department", default="", help="部门")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--seed-demo", action="store_true", help="创建三个演示账号")
    args = parser.parse_args()

    if args.seed_demo:
        for username, display_name, role, department, password in DEMO_ACCOUNTS:
            database.create_user(username, display_name, role, auth.hash_password(password),
                                 department)
            print(f"已创建：{username} / {password}（{display_name}，{role}）")
        return 0

    if args.create:
        if not (args.name and args.role and args.password):
            raise SystemExit("--create 需要同时指定 --name --role --password")
        database.create_user(args.create, args.name, args.role,
                             auth.hash_password(args.password), args.department)
        print(f"已创建/更新：{args.create}（{args.name}，{args.role}）")
        return 0

    users = database.list_users()
    if not users:
        print("还没有任何账号，先跑：python scripts/manage_users.py --seed-demo")
        return 0
    print(f"{'用户名':<14}{'显示名':<12}{'角色':<10}部门")
    print("-" * 50)
    for user in users:
        print(f"{user['username']:<14}{user['display_name']:<12}{user['role']:<10}{user['department']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
