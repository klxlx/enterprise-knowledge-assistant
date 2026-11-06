"""分块策略的单元测试（纯函数，不访问网络）。"""

import config_data as config
from chunking import split_document


def test_markdown_headings_become_sections():
    text = "# 第一章\n\n" + "内容甲。" * 40 + "\n\n# 第二章\n\n" + "内容乙。" * 40
    chunks = split_document(text, "测试手册")

    assert {chunk.section for chunk in chunks} == {"第一章", "第二章"}
    assert all(chunk.embed_text.startswith("测试手册 · ") for chunk in chunks)
    assert all(chunk.embed_text.split("\n", 1)[1].strip() for chunk in chunks)


def test_long_section_is_split_and_context_is_wider_than_chunk():
    text = "# 说明\n\n" + "这是一句用于测试的文本。" * 300
    chunks = split_document(text, "长文档")

    assert len(chunks) > 1
    body_lengths = [len(chunk.embed_text.split("\n", 1)[1]) for chunk in chunks]
    assert all(length <= config.chunk_size for length in body_lengths)
    assert any(len(chunk.context_text) > config.chunk_size for chunk in chunks)


def test_bracket_headings_are_recognized():
    text = "【保养】\n滤网要晾干。\n【故障】\n回不了充电座先擦传感器。"
    chunks = split_document(text, "手册")

    assert {chunk.section for chunk in chunks} == {"保养", "故障"}


def test_plain_text_without_headings_still_splits():
    chunks = split_document("没有标题的正文。" * 100, "无标题文档")

    assert chunks
    assert chunks[0].section == ""
    assert all(chunk.embed_text.startswith("无标题文档\n") for chunk in chunks)


def test_blank_text_returns_no_chunks():
    assert split_document("   \n\n  ", "空文档") == []
