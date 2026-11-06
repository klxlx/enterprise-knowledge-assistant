"""检索与问答效果评测。

三种模式：
    python scripts/evaluate.py                 只评测检索链路，秒级完成，不消耗模型额度
    python scripts/evaluate.py --with-answer   检索 + 生成，评测"直连检索"这条基线的答案质量
    python scripts/evaluate.py --agent         走带工具的完整链路，统计工具调用正确率

改动检索参数、分块策略、提示词或工具定义之后跑一遍，对比指标是涨还是跌。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path

# 评测产生的会话历史写到临时目录，不污染项目里的 chat_history/。
os.environ.setdefault("RAG_DATA_DIR", tempfile.mkdtemp(prefix="rag-eval-"))

import platform_setup  # noqa: F401  必须先于 vector_stores / rag 导入
import config_data as config
import database
from database import GENERIC_MODEL, ROLE_NEWCOMER, ROLE_STAFF
from rag import RagService

DATASET_PATH = Path(__file__).resolve().parent.parent / "eval_dataset.json"
# 模型可能用"没查到资料"或"不在服务范围"两种方式拒答，两种都算正确。
REFUSAL_MARKERS = (
    "没有查到", "查不到", "没有相关", "没有找到", "未找到", "无法回答", "资料中没有",
    "不在我的服务范围", "不在服务范围", "不在服务范围内", "不在这项服务范围",
    "无法帮您解答", "无法解答", "不属于", "无法提供", "超出",
    # 下面几种是模型实际用过的同义说法：拒绝的意思到位了，只是措辞没被上面的词表覆盖
    "不在我服务", "无法为您提供", "无法为您解答", "不属于我的服务",
    # 权限不足也是"这轮拿不到资料"，但原因不是知识库缺内容
    "仅员工可见", "没有查看权限", "没有权限", "权限不足", "需要员工账号",
)
# 词表覆盖不到的长句用正则兜底：中间插了定语也仍然算"不在服务范围"
REFUSAL_PATTERNS = (
    re.compile(r"不在.{0,16}服务范围"),
    re.compile(r"无法(为您)?(提供|解答|回答)"),
)


def is_refusal(answer: str) -> bool:
    """回答是否明确拒答/说明查不到。"""
    normalized = normalize(answer)
    if any(marker in normalized for marker in REFUSAL_MARKERS):
        return True
    return any(pattern.search(normalized) for pattern in REFUSAL_PATTERNS)
# 答案要点用关键词判据，模型换个说法就会漏判，所以整体达到这个比例就算通过
ANSWER_PASS_RATE = 0.9


def load_cases() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def knowledge_base_files() -> list[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT filename FROM rag_documents ORDER BY filename"
        ).fetchall()
    return [row[0] for row in rows]


def normalize(text: str) -> str:
    """去掉所有空白，避免模型在关键词中间插入换行或空格导致漏判。"""
    return "".join(text.split())


def keyword_hit(keyword, text: str) -> bool:
    """关键词命中判定：字符串按子串匹配，数组表示多个同义说法，命中任意一个即可。

    两边都做去空白处理，否则判据写「没有 X99」、回答里却是「没有X99」就会漏判。
    """
    haystack = normalize(text)
    if isinstance(keyword, (list, tuple)):
        return any(normalize(str(alternative)) in haystack for alternative in keyword)
    return normalize(str(keyword)) in haystack


def keyword_label(keyword) -> str:
    return "/".join(str(item) for item in keyword) if isinstance(keyword, (list, tuple)) else str(keyword)


def print_header(cases: list[dict], mode: str) -> None:
    files = knowledge_base_files()
    print(f"知识库文件（{len(files)}）: " + ("、".join(files) if files else "空"))
    expected_sources = {
        case["expected_source"] for case in cases
        if case.get("expected_source") and case.get("expected_keywords")
    }
    missing = sorted(expected_sources - set(files))
    if missing:
        print("警告：评测集期望的来源不在知识库里：" + "、".join(missing))
    print(f"模式: {mode}")
    print("-" * 96)


def evaluate_retrieval(service: RagService, cases: list[dict], with_answer: bool,
                       default_role: str) -> list[str]:
    """直连检索：不经过模型决策，直接看检索命中和答案要点。"""
    skipped = [case for case in cases if case.get("agent_only")]
    cases = [case for case in cases if not case.get("agent_only")]
    print_header(cases, "检索直连" + ("（含答案生成）" if with_answer else ""))
    if skipped:
        print(f"跳过 {len(skipped)} 条只在工具链路下成立的用例："
              + "、".join(case["id"] for case in skipped))
    if with_answer:
        print("提示：直连检索不抽取产品型号，型号类用例的答案会失败——"
              "这正是工具链路（模型自己填 scope）的优势。")
    print(f"检索参数: k={config.retrieval_k}  score_threshold={config.score_threshold}  "
          f"重排序={'开' if config.rerank_enabled else '关'}")

    failures: list[str] = []
    hits_count = 0
    answer_count = 0
    returned: list[int] = []
    retrieval_ms: list[float] = []
    answer_ms: list[float] = []

    for case in cases:
        role = case.get("role", default_role)
        started = time.perf_counter()
        chunks = service.search(case["question"], None, role)
        retrieval_ms.append((time.perf_counter() - started) * 1000)

        joined = normalize("\n".join(chunk.content for chunk in chunks))
        keywords = case.get("expected_keywords") or []
        top = chunks[0] if chunks else None
        returned.append(len(chunks))

        if case.get("expect_no_answer"):
            passed = not chunks
            detail = "检索为空" if passed else f"召回了 {len(chunks)} 条（top1={top.source} {top.score:.3f}）"
        else:
            hits = [keyword for keyword in keywords if keyword_hit(keyword, joined)]
            passed = len(hits) == len(keywords)
            detail = f"要点 {len(hits)}/{len(keywords)}"
            detail += f"，top1={top.source} {top.score:.3f}" if top else "，未召回"

        forbidden = case.get("forbidden_source")
        if forbidden:
            leaked = any(chunk.source == forbidden for chunk in chunks)
            passed &= not leaked
            detail += "，未泄漏内部资料" if not leaked else f"，泄漏了内部资料：{forbidden}"

        hits_count += int(passed)
        line = f"[{'OK ' if passed else 'FAIL'}] {case['id']:<24} {detail}"

        if with_answer:
            started = time.perf_counter()
            answer = normalize("".join(
                service.stream_answer(case["question"], chunks, f"eval-{case['id']}")
            ))
            answer_ms.append((time.perf_counter() - started) * 1000)
            if keywords:
                hit_count = sum(1 for keyword in keywords if keyword_hit(keyword, answer))
                answer_pass = hit_count == len(keywords)
                answer_count += int(answer_pass)
                line += f" | 答案要点 {hit_count}/{len(keywords)}"
                if not answer_pass:
                    failures.append(f"{case['id']}(答案)")
            else:
                refused = is_refusal(answer)
                answer_count += int(refused)
                line += f" | {'已说明查不到' if refused else '未说明查不到'}"
                if not refused:
                    failures.append(f"{case['id']}(答案)")

        if not passed:
            failures.append(case["id"])
        print(line)

    total = len(cases)
    print("-" * 96)
    print(f"检索通过     : {hits_count}/{total}  ({hits_count / total:.0%})")
    print(f"平均返回片段 : {sum(returned) / len(returned):.2f} 条/问")
    print(f"平均检索耗时 : {sum(retrieval_ms) / len(retrieval_ms):.0f} ms")
    if with_answer:
        print(f"答案通过     : {answer_count}/{total}  ({answer_count / total:.0%})")
        print(f"平均回答耗时 : {sum(answer_ms) / len(answer_ms):.0f} ms")
    return failures


def evaluate_agent(service: RagService, cases: list[dict], default_role: str) -> list[str]:
    """完整链路：让模型自己决定调用哪些工具，统计工具调用正确率。"""
    print_header(cases, "工具链路（模型自主决定是否调用工具）")
    print(f"最多工具轮数: {config.max_tool_rounds}  思考模式: "
          f"{'关闭' if config.disable_thinking else '开启'}")
    print("-" * 96)

    failures: list[str] = []
    tool_checked = tool_ok = 0
    answer_checked = answer_ok = 0
    negative_checked = negative_ok = 0
    tool_calls_total = 0
    model_checked = model_ok = 0
    file_checked = file_ok = 0
    elapsed_list: list[float] = []

    for case in cases:
        role = case.get("role", default_role)
        started = time.perf_counter()
        tools: list[dict] = []
        sources: list[dict] = []
        files: list[dict] = []
        answer_parts: list[str] = []
        faq_hit = False
        for event in service.stream_events(case["question"], f"eval-{case['id']}", role):
            if event["type"] == "tool":
                tools.append({"name": event["name"], "arguments": event.get("arguments", {})})
            elif event["type"] == "sources":
                sources = event["sources"]
            elif event["type"] == "files":
                files = event["files"]
            elif event["type"] == "content":
                answer_parts.append(event["text"])
            elif event["type"] == "handoff":
                handoff = event["handoff"]
            elif event["type"] == "faq":
                faq_hit = True
        elapsed = time.perf_counter() - started
        elapsed_list.append(elapsed)
        tool_calls_total += len(tools)

        answer = normalize("".join(answer_parts))
        keywords = case.get("expected_keywords") or []
        expected_tool = case.get("expected_tool")
        details = []
        passed = True        # 硬性检查：工具、参数、拒答、资料隔离
        keyword_ok = True    # 软性检查：答案要点，整体达阈值即可

        if case.get("expect_faq"):
            passed &= faq_hit
            details.append("FAQ 直连" if faq_hit else "未走 FAQ 直连")
            if faq_hit:
                details.append(f"{elapsed * 1000:.0f}ms")

        if expected_tool:
            tool_checked += 1
            called = any(item["name"] == expected_tool for item in tools)
            tool_ok += int(called)
            passed &= called
            details.append(f"工具={expected_tool}" if called else f"工具未调用（期望 {expected_tool}）")
            if called:
                call = next(item for item in tools if item["name"] == expected_tool)
                for key, value in (case.get("expected_tool_args") or {}).items():
                    arg_ok = str(value) in str(call["arguments"].get(key, ""))
                    passed &= arg_ok
                    details.append(f"参数{key}={'正确' if arg_ok else '不符'}")

        if keywords:
            answer_checked += 1
            hit_count = sum(1 for keyword in keywords if keyword_hit(keyword, answer))
            keyword_ok = hit_count == len(keywords)
            answer_ok += int(keyword_ok)
            missed = [keyword_label(item) for item in keywords if not keyword_hit(item, answer)]
            details.append(f"答案要点 {hit_count}/{len(keywords)}")
            if missed:
                details.append("措辞未命中:" + "、".join(missed))
        elif case.get("expect_no_answer"):
            negative_checked += 1
            refused = is_refusal(answer)
            negative_ok += int(refused)
            passed &= refused
            details.append("已说明查不到" if refused else "未说明查不到")

        if sources and case.get("expected_source"):
            source_hit = any(item["source"] == case["expected_source"] for item in sources)
            details.append("来源命中" if source_hit else "来源不符")

        forbidden = case.get("forbidden_source")
        if forbidden:
            leaked = any(item["source"] == forbidden for item in sources)
            passed &= not leaked
            details.append("未泄漏内部资料" if not leaked else f"泄漏内部资料：{forbidden}")

        # 敏感信息（例如只对员工可见的员工姓名、分机号）不能出现在新人的回答里
        forbidden_words = case.get("forbidden_keywords") or []
        if forbidden_words:
            leaked_words = [word for word in forbidden_words
                            if normalize(word) in normalize(answer)]
            passed &= not leaked_words
            details.append("未泄漏内部信息" if not leaked_words
                           else f"泄漏内部信息：{'、'.join(leaked_words)}")

        expected_file = case.get("expected_file")
        if expected_file:
            file_checked += 1
            file_hit = any(expected_file in item["name"] for item in files)
            file_ok += int(file_hit)
            passed &= file_hit
            details.append("文件命中" if file_hit else f"文件未命中（期望 {expected_file}）")

        # 范围隔离：指定适用范围时，来源只允许是该范围 + 通用资料
        wanted_model = (case.get("expected_tool_args") or {}).get("scope")
        if wanted_model and sources:
            # 与检索同样的语义：来源适用范围包含所查范围（或为通用）都算命中，
            # 例如查 T30 时命中「DEEBOT T30 COMBO」是合理的。
            wanted = wanted_model.lower()

            def allowed(item: dict) -> bool:
                scope_value = (item.get("scope") or GENERIC_MODEL)
                return scope_value == GENERIC_MODEL or wanted in scope_value.lower()

            leaked = [item["source"] for item in sources if not allowed(item)]
            model_checked += 1
            model_ok += int(not leaked)
            if leaked:
                passed = False
                details.append("范围串答:" + "、".join(leaked[:2]))
            else:
                details.append("范围隔离OK")

        if not passed:
            failures.append(case["id"])
        status = "OK " if passed and keyword_ok else ("WARN" if passed else "FAIL")
        line = f"[{status}] {case['id']:<24} {'　'.join(details)}"
        line += f"　{elapsed:.1f}s"
        print(line)
        if not passed:
            print(f"       实际回答：{answer[:160]}")

    total = len(cases)
    print("-" * 96)
    if tool_checked:
        print(f"工具调用正确率 : {tool_ok}/{tool_checked}  ({tool_ok / tool_checked:.0%})")
    if answer_checked:
        rate = answer_ok / answer_checked
        note = "" if rate >= ANSWER_PASS_RATE else f"  ← 低于阈值 {ANSWER_PASS_RATE:.0%}"
        print(f"答案要点命中   : {answer_ok}/{answer_checked}  ({rate:.0%}){note}")
        if rate < ANSWER_PASS_RATE:
            failures.append(f"答案要点整体低于 {ANSWER_PASS_RATE:.0%}")
    if negative_checked:
        print(f"无关问题拒答   : {negative_ok}/{negative_checked}  ({negative_ok / negative_checked:.0%})")
    if model_checked:
        print(f"范围隔离正确   : {model_ok}/{model_checked}  ({model_ok / model_checked:.0%})")
    if file_checked:
        print(f"文件检索正确   : {file_ok}/{file_checked}  ({file_ok / file_checked:.0%})")
    print(f"平均工具调用   : {tool_calls_total / total:.2f} 次/问")
    print(f"平均端到端耗时 : {sum(elapsed_list) / len(elapsed_list):.1f} s")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 与工具调用效果评测")
    parser.add_argument("--with-answer", action="store_true",
                        help="检索链路下额外评测答案（消耗模型额度）")
    parser.add_argument("--agent", action="store_true",
                        help="走带工具的完整链路，统计工具调用正确率（消耗模型额度）")
    parser.add_argument("--role", choices=[ROLE_STAFF, ROLE_NEWCOMER], default=ROLE_STAFF,
                        help="以什么角色评测；用例里单独写 role 的以用例为准")
    parser.add_argument("--only",
                        help="只跑指定用例，逗号分隔，例如 out-of-scope-passport,dingtalk-strategy")
    args = parser.parse_args()

    cases = load_cases()
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            raise SystemExit(f"没有匹配的用例：{args.only}")
    service = RagService()

    if args.agent:
        failures = evaluate_agent(service, cases, args.role)
    else:
        failures = evaluate_retrieval(service, cases, args.with_answer, args.role)

    print()
    if failures:
        print(f"未通过用例（{len(failures)}）: " + "、".join(failures))
        return 1
    print("全部用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
