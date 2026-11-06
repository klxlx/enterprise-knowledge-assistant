"""把 CSV 里的员工卡片和结构化知识（FAQ / 流程）导入数据库。

用法：
    python scripts/import_handbook.py --employees data/employees.csv
    python scripts/import_handbook.py --handbook data/handbook.csv
    python scripts/import_handbook.py --employees data/employees.csv --handbook data/handbook.csv --list

CSV 里的多值字段用 `|` 分隔（关键词、步骤、链接）。重复导入会按主键更新，不会产生重复记录。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import csv
from pathlib import Path

import database
from database import ROLE_NEWCOMER, ROLE_STAFF


def split_multi(value: str) -> list[str]:
    return [piece.strip() for piece in (value or "").split("|") if piece.strip()]


def import_employees(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        database.upsert_employee({
            "employee_id": row["employee_id"].strip(),
            "name": row["name"].strip(),
            "department": row["department"].strip(),
            "title": (row.get("title") or "").strip(),
            "employee_type": (row.get("employee_type") or "员工").strip(),
            "responsibilities": (row.get("responsibilities") or "").strip(),
            "contact": (row.get("contact") or "").strip(),
            "keywords": (row.get("keywords") or "").strip(),
            "scope": (row.get("scope") or "").strip(),
            "visibility": (row.get("visibility") or "").strip(),
        })
    return len(rows)


def import_handbook(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        database.upsert_handbook_entry({
            "entry_id": row["entry_id"].strip(),
            "kind": row["kind"].strip(),
            "title": row["title"].strip(),
            "question": (row.get("question") or "").strip(),
            "answer": (row.get("answer") or "").strip(),
            "steps": split_multi(row.get("steps")),
            "links": split_multi(row.get("links")),
            "keywords": (row.get("keywords") or "").strip(),
            "scope": (row.get("scope") or "").strip(),
            "visibility": (row.get("visibility") or "").strip(),
            "owner": (row.get("owner") or "").strip(),
        })
    return len(rows)


def print_summary() -> None:
    entries = database.list_handbook_entries(role=ROLE_STAFF)
    faq = [item for item in entries if item["kind"] == "faq"]
    process = [item for item in entries if item["kind"] == "process"]
    employees = database.list_employees(role=ROLE_STAFF)
    print(f"\n当前库内容：FAQ {len(faq)} 条、流程 {len(process)} 条、员工卡片 {len(employees)} 张")

    visible_to_newcomer = [item for item in employees if item["visibility"] == "all"]
    print(f"其中新人可见：员工 {len(visible_to_newcomer)} 张、"
          f"FAQ {len([i for i in faq if i['visibility'] == 'all'])} 条")
    print("\n按部门分组：")
    for department in sorted({item["department"] for item in employees}):
        names = [item["name"] for item in employees if item["department"] == department]
        print(f"  {department}：{'、'.join(names)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="导入员工卡片与结构化知识")
    parser.add_argument("--employees", help="员工卡片 CSV 路径")
    parser.add_argument("--handbook", help="FAQ / 流程 CSV 路径")
    parser.add_argument("--list", action="store_true", help="导入后打印库里内容")
    args = parser.parse_args()

    if not args.employees and not args.handbook:
        parser.error("至少要指定 --employees 或 --handbook")

    if args.employees:
        count = import_employees(Path(args.employees))
        print(f"已导入员工卡片 {count} 张")
    if args.handbook:
        count = import_handbook(Path(args.handbook))
        print(f"已导入结构化知识 {count} 条")
    if args.list or args.employees or args.handbook:
        print_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
