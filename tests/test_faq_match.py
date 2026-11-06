"""FAQ 快速匹配的单元测试（纯函数，不访问数据库）。"""

from database import match_faq


def entry(entry_id: str, keywords: str) -> dict:
    return {"entry_id": entry_id, "keywords": keywords}


def test_requires_at_least_two_keyword_hits():
    entries = [entry("faq-apply-pc", "电脑|账号|申请")]

    # 命中「申请 + 电脑 + 账号」→ 直连
    assert match_faq("新员工怎么申请电脑和账号？", entries)["entry_id"] == "faq-apply-pc"
    # 只命中「电脑」→ 不直连，避免"电脑连不上网"误命中"申请电脑"
    assert match_faq("我的电脑连不上网了", entries) is None


def test_prefers_entry_with_more_hits_then_fewer_keywords():
    entries = [entry("few", "报销|发票"), entry("many", "报销|发票|差旅")]

    # 都命中 2 个关键词时，取关键词更少的那条（更具体）
    assert match_faq("报销发票怎么弄", entries)["entry_id"] == "few"
    # 命中 3 个关键词的那条更匹配
    assert match_faq("出差差旅的报销发票怎么弄", entries)["entry_id"] == "many"


def test_no_match_returns_none():
    entries = [entry("faq-1", "入职|材料")]

    assert match_faq("今天天气怎么样", entries) is None
    assert match_faq("", entries) is None
