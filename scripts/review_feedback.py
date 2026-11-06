"""反馈与知识盲区清单。

用法：
    python scripts/review_feedback.py                概览 + 盲区清单（最近 30 天）
    python scripts/review_feedback.py --days 7       只看最近一周
    python scripts/review_feedback.py --negative     只看被点「没用」的回答明细
    python scripts/review_feedback.py --limit 100    清单最多显示多少条

盲区口径写在 database.list_blind_spots() 里：
  1. evidence 为空——这轮既没命中 FAQ/流程/员工卡片，也没检索到任何资料，纯靠模型常识答的；
  2. 触发了转人工——知识库接不住；
  3. 用户点了「没用」。
补齐资料后，同一批问题再来问一遍，就能看到盲区条数下降。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse

import database


def print_overview(days: int) -> None:
    stats = database.feedback_stats(days=days)
    print(f"最近 {days} 天问答概览")
    print("-" * 78)
    print(f"  总轮数           : {stats['turns']}")
    print(f"  有效反馈         : {stats['rated']}（好评 {stats['positive']} / 差评 {stats['negative']}）")
    if stats["satisfaction"] is None:
        print("  好评率           : 暂无反馈")
    else:
        print(f"  好评率           : {stats['satisfaction']:.0%}")
    print(f"  无依据回答       : {stats['unsupported']}"
          "（没有任何资料支撑，制度类问题最危险）")
    print(f"  转人工           : {stats['handoffs']}")
    print()


def print_blind_spots(days: int, limit: int) -> None:
    spots = database.list_blind_spots(days=days, limit=limit)
    print(f"知识盲区清单（最近 {days} 天，共 {len(spots)} 个问题）")
    print("-" * 78)
    if not spots:
        print("  没有盲区，知识库暂时接得住这些问题。")
        return
    print(f"{'次数':>4}  {'差评':>4}  {'转人工':>6}  问题")
    for spot in spots:
        print(f"{spot['times']:>4}  {spot['negative']:>4}  {spot['handoffs']:>6}  "
              f"{spot['question'][:52]}")
        answer = (spot["last_answer"] or "").replace("\n", " ")
        if answer:
            print(f"        最近一次回答：{answer[:70]}")
    print()
    print("建议：优先补出现次数最多的那几条；补完资料后可以拿同样的问题再问一遍验证。")


def print_negative(days: int, limit: int) -> None:
    entries = database.list_feedback_entries(days=days, rating=-1, limit=limit)
    print(f"被点「没用」的回答（最近 {days} 天，共 {len(entries)} 条）")
    print("-" * 78)
    if not entries:
        print("  暂时没有差评。")
        return
    for entry in entries:
        who = entry["username"] or "未登录"
        print(f"\n#{entry['id']}  {entry['created_at'][:19]}  {who}（{entry['role']}）")
        print(f"  问题：{entry['question']}")
        print(f"  回答：{(entry['answer'] or '').replace(chr(10), ' ')[:100]}")
        print(f"  依据：{entry['evidence'] or '无（没有任何资料支撑）'}")
        if entry["comment"]:
            print(f"  用户说：{entry['comment']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看用户反馈和知识盲区清单")
    parser.add_argument("--days", type=int, default=30, help="统计最近多少天，默认 30")
    parser.add_argument("--limit", type=int, default=50, help="最多显示多少条")
    parser.add_argument("--negative", action="store_true",
                        help="只看被点「没用」的回答明细")
    parser.add_argument("--no-overview", action="store_true", help="不打印概览")
    args = parser.parse_args()

    if not args.no_overview:
        print_overview(args.days)
    if args.negative:
        print_negative(args.days, args.limit)
    else:
        print_blind_spots(args.days, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
