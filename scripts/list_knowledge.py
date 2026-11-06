"""知识库维护工具。

用法：
    python scripts/list_knowledge.py                       列出所有文件
    python scripts/list_knowledge.py --by-scope            按适用范围分组
    python scripts/list_knowledge.py --content             连正文一起打印
    python scripts/list_knowledge.py --file 名字.txt        只看某个文件
    python scripts/list_knowledge.py --check               体检：找出「正文提到的适用范围和标签不符」的资料
    python scripts/list_knowledge.py --set-scope 旧文件.pdf --scope "DEEBOT T20"
    python scripts/list_knowledge.py --set-validity 制度.txt --expires-at 2026-12-31

改适用范围不需要重新向量化：它只用于检索过滤，不参与嵌入。
改时效同理，只影响检索过滤；失效日期过了的资料会直接退出检索结果。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import re
from datetime import date

import database
from database import GENERIC_MODEL, NO_EXPIRY_DATE

# 体检用的型号模样：DEEBOT 开头的、以及 T20 / X5 这类短型号
MODEL_TOKEN = re.compile(
    r"DEEBOT\s+[A-Z][A-Z0-9]*(?:\s+[A-Z][A-Z0-9]*){0,2}|(?<![A-Za-z0-9])(?:T|X)\d{1,2}(?![0-9])"
)
MIN_MENTIONS = 2
# 比较型号时把品牌词去掉，"DEEBOT T20" 和正文里的 "T20" 要算同一个型号
BRAND_WORDS = ("deebot", "ecovacs", "科沃斯")


def model_tokens(text: str) -> list[str]:
    """正文里反复出现的型号：按去掉品牌词后的核心归并计数。

    「DEEBOT T20」和「T20」是同一个型号，必须合并计数，否则会漏判。
    """
    counter: dict[str, int] = {}
    display: dict[str, str] = {}
    for match in MODEL_TOKEN.finditer(text):
        token = " ".join(match.group(0).split())
        core = model_core(token)
        counter[core] = counter.get(core, 0) + 1
        if core not in display or len(token) > len(display[core]):
            display[core] = token
    return [display[core] for core, count in counter.items() if count >= MIN_MENTIONS]


def own_scopes(tag: str) -> list[str]:
    return [piece.strip() for piece in database.normalize_scope(tag).split(",") if piece.strip()]


def model_core(text: str) -> str:
    core = text.lower()
    for brand in BRAND_WORDS:
        core = core.replace(brand, "")
    return "".join(core.split())


def is_own(token: str, own: list[str]) -> bool:
    """判断正文里的型号字样是不是自己标签的一部分。

    「X20」和「X20 Pro」算同一个系列，「DEEBOT X5」和「DEEBOT X5 MAX」也算，
    所以任何一方是另一方的前缀都视为自己的型号。
    """
    token = model_core(token)
    return any(
        model == token or model.startswith(token) or token.startswith(model)
        for model in (model_core(item) for item in own)
    )


def validity_text(effective_from, expires_at) -> str:
    """一行展示资料时效：长期有效的只写生效日，有期限的写成区间。"""
    start = effective_from.isoformat() if effective_from else "未标注"
    if not expires_at or expires_at.isoformat() == NO_EXPIRY_DATE:
        return f"{start} 起长期有效"
    return f"{start} → {expires_at.isoformat()}"


def parse_date(value: str | None, field: str) -> str | None:
    """命令行日期参数校验，格式不对直接退出并说清原因。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise SystemExit(f"{field} 需要 YYYY-MM-DD 格式的日期：{value}") from exc


def print_table(documents: list[tuple]) -> None:
    print(f"知识库共 {len(documents)} 个文件：\n")
    print(f"{'文件名':<32}{'适用范围':<24}{'片段':>4}{'字数':>7}  时效")
    print("-" * 92)
    today = date.today()
    for filename, model, chunk_count, created_at, characters, effective, expires in documents:
        stamp = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "-"
        expired = expires and expires < today
        mark = "[已过期] " if expired else ""
        print(f"{filename:<32}{model:<24}{chunk_count:>4}{characters:>7}  "
              f"{mark}{validity_text(effective, expires)}（入库 {stamp}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看和维护知识库")
    parser.add_argument("--content", action="store_true", help="打印每个片段的正文")
    parser.add_argument("--file", help="只看指定文件名的片段")
    parser.add_argument("--by-scope", action="store_true", help="按适用范围分组列出")
    parser.add_argument("--check", action="store_true", help="体检：找出标签和正文不符的资料")
    parser.add_argument("--set-scope", metavar="文件名", help="修改某个文件的适用范围")
    parser.add_argument("--scope", help="配合 --set-scope 的新适用范围；多个用逗号分隔")
    parser.add_argument("--visibility", choices=["all", "staff"],
                        help="配合 --set-scope 改可见性：all=新人也能看，staff=仅员工可见")
    parser.add_argument("--set-validity", metavar="文件名",
                        help="修改某个文件的生效/失效日期")
    parser.add_argument("--effective-from", metavar="YYYY-MM-DD",
                        help="配合 --set-validity 改生效日期")
    parser.add_argument("--expires-at", metavar="YYYY-MM-DD",
                        help="配合 --set-validity 改失效日期；填 9999-12-31 表示长期有效")
    args = parser.parse_args()

    if args.set_scope:
        if not args.scope and not args.visibility:
            raise SystemExit("--set-scope 需要同时指定 --scope 或 --visibility")
        changed = False
        if args.scope:
            documents, chunks = database.set_scope(args.set_scope, args.scope)
            if documents:
                changed = True
                print(f"  适用范围 → {database.normalize_scope(args.scope)}"
                      f"（文档 {documents} 条、片段 {chunks} 条）")
        if args.visibility:
            documents, chunks = database.set_visibility(args.set_scope, args.visibility)
            if documents:
                changed = True
                print(f"  可见性 → {args.visibility}（文档 {documents} 条、片段 {chunks} 条）")
        if not changed:
            raise SystemExit(f"没有找到当前版本的资料：{args.set_scope}")
        print(f"已更新：{args.set_scope}（无需重新向量化）")
        return 0

    if args.set_validity:
        if not args.effective_from and not args.expires_at:
            raise SystemExit("--set-validity 需要同时指定 --effective-from 或 --expires-at")
        effective = parse_date(args.effective_from, "--effective-from")
        expires = parse_date(args.expires_at, "--expires-at")
        if effective and expires and expires < effective:
            raise SystemExit("失效日期不能早于生效日期")
        documents, chunks = database.set_validity(
            args.set_validity, effective, expires)
        if not documents:
            raise SystemExit(f"没有找到当前版本的资料：{args.set_validity}")
        print(f"已更新时效：{args.set_validity}"
              f"（文档 {documents} 条、片段 {chunks} 条）")
        changes = []
        if effective:
            changes.append(f"生效日期={effective}")
        if expires:
            changes.append("失效日期=长期有效" if expires == NO_EXPIRY_DATE
                           else f"失效日期={expires}")
        print("  " + "，".join(changes))
        return 0

    with database.connect() as connection:
        documents = connection.execute(
            """
            SELECT d.filename, d.scope, d.chunk_count, d.created_at,
                   coalesce(sum(length(c.content)), 0),
                   d.effective_from, d.expires_at
            FROM rag_documents d
            LEFT JOIN rag_chunks c ON c.document_hash = d.document_hash
            WHERE d.is_current
            GROUP BY d.filename, d.scope, d.chunk_count, d.created_at,
                     d.effective_from, d.expires_at
            ORDER BY d.scope, d.created_at
            """
        ).fetchall()

        if not documents:
            print("知识库是空的，还没有上传任何文件。")
            return 0

        if args.check:
            return run_check(connection, documents)

        if args.by_scope:
            for model in sorted({row[1] for row in documents}):
                group = [row for row in documents if row[1] == model]
                print(f"\n【{model}】{len(group)} 份")
                for filename, _, chunk_count, _, characters, effective, expires in group:
                    print(f"  {filename}（{chunk_count} 段 / {characters} 字 / "
                          f"{validity_text(effective, expires)}）")
            return 0

        print_table(documents)

        if not (args.content or args.file):
            print("\n想看正文就加 --content；按适用范围分组加 --by-scope；体检加 --check")
            return 0

        chunks = connection.execute(
            """
            SELECT d.filename, c.metadata->>'chunk_index', c.content
            FROM rag_chunks c JOIN rag_documents d ON d.document_hash = c.document_hash
            WHERE c.is_current AND (%s::text IS NULL OR d.filename = %s::text)
            ORDER BY d.filename, (c.metadata->>'chunk_index')::int
            """,
            (args.file, args.file),
        ).fetchall()

    for filename, chunk_index, content in chunks:
        print("\n" + "=" * 74)
        print(f"[{filename}] 第 {int(chunk_index) + 1} 段")
        print("-" * 74)
        print(content)
    return 0


def run_check(connection, documents: list[tuple]) -> int:
    """体检：正文里反复提到、但不在自己标签里的适用范围，很可能是贴错了。"""
    print("适用范围体检（正文提到 ≥2 次的型号，与标签对比）\n")
    all_scopes = sorted({
        piece.strip()
        for _, scope_value, *_ in documents
        for piece in database.normalize_scope(scope_value).split(",")
        if piece.strip() and piece.strip() != GENERIC_MODEL
    })
    suspects = []
    for filename, scope_value, *_ in documents:
        contents = connection.execute(
            """
            SELECT c.content FROM rag_chunks c
            JOIN rag_documents d ON d.document_hash = c.document_hash
            WHERE d.filename = %s AND d.is_current AND c.is_current
            """,
            (filename,),
        ).fetchall()
        text = "\n".join(row[0] for row in contents)
        own = own_scopes(scope_value)
        mentioned = {token for token in model_tokens(text) if not is_own(token, own)}
        # 除了型号，其它适用范围（例如部门名）出现在正文里也算可疑
        lowered = text.lower()
        for candidate in all_scopes:
            if not is_own(candidate, own) and candidate.lower() in lowered:
                mentioned.add(candidate)
        others = sorted(mentioned)
        if others:
            suspects.append((filename, scope_value, others))
        print(f"[{'可疑' if others else '正常'}] {filename}")
        print(f"        当前标签：{scope_value}")
        if others:
            print(f"        正文提到：{'、'.join(others)}")

    print()
    if suspects:
        print(f"发现 {len(suspects)} 份资料可能贴错了适用范围，可以用下面的命令修正：")
        for filename, _, _ in suspects:
            print(f'  python scripts/list_knowledge.py --set-scope "{filename}" --scope "正确适用范围"')
        return 1
    print("没有发现明显贴错的资料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
