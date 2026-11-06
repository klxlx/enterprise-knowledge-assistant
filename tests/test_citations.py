"""引用编号收口的单元测试（纯函数，不访问网络）。"""

from rag import _split_partial_marker, _strip_citations


def test_repeated_citation_is_collected_once_and_removed():
    citations: list[str] = []

    cleaned = _strip_citations("先清理传感器【1】。再检查遮挡物【1】。", citations)

    assert cleaned == "先清理传感器。再检查遮挡物。"
    assert citations == ["【1】"]


def test_multiple_sources_keep_their_order():
    citations: list[str] = []

    cleaned = _strip_citations("前半句【2】后半句【1】", citations)

    assert cleaned == "前半句后半句"
    assert citations == ["【2】", "【1】"]


def test_partial_marker_is_held_until_next_chunk():
    assert _split_partial_marker("答案是") == ("答案是", "")
    assert _split_partial_marker("答案是【") == ("答案是", "【")
    assert _split_partial_marker("答案是【1") == ("答案是", "【1")
    assert _split_partial_marker("答案是【1】") == ("答案是【1】", "")


def test_long_bracket_text_is_not_treated_as_marker():
    text = "【这里是普通的一段括号说明文字"

    assert _split_partial_marker(text) == (text, "")


def test_text_without_citation_is_unchanged():
    citations: list[str] = []

    assert _strip_citations("普通回答，没有任何编号。", citations) == "普通回答，没有任何编号。"
    assert citations == []
