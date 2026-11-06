"""知识库维护工具的单元测试（纯函数）。"""

from scripts.list_knowledge import is_own, model_core, model_tokens


def test_model_core_strips_brand_words():
    assert model_core("DEEBOT X5 MAX") == "x5max"
    assert model_core("科沃斯 T20") == "t20"
    assert model_core("X20 Pro") == "x20pro"


def test_is_own_treats_family_and_brand_as_same_model():
    # 正文写 X20，标签写 X20 Pro：同系列，不算冲突
    assert is_own("X20", ["X20 Pro"]) is True
    # 正文写 T20，标签写 DEEBOT T20：只差品牌词
    assert is_own("T20", ["DEEBOT T20"]) is True
    # 一份手册同时覆盖两个型号
    assert is_own("X5", ["DEEBOT X5 MAX", "DEEBOT X5 PRO"]) is True


def test_is_own_flags_different_models():
    assert is_own("T20", ["T30"]) is False
    assert is_own("X5", ["DEEBOT T20"]) is False


def test_model_tokens_needs_repeated_mentions():
    text = "DEEBOT T20 说明书。T20 支持 2.4G 网络。T30 只出现一次。"

    tokens = model_tokens(text)

    # 「DEEBOT T20」和「T20」是同一个型号，合并算两次
    assert any(model_core(token) == "t20" for token in tokens)
    # 只出现一次的型号不算
    assert all(model_core(token) != "t30" for token in tokens)
