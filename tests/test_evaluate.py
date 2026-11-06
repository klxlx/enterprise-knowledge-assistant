"""评测脚本判据的单元测试。"""

from scripts.evaluate import is_refusal, keyword_hit, keyword_label, normalize


def test_keyword_hit_matches_substring():
    assert keyword_hit("防水鞋", "建议穿防水鞋出门") is True
    assert keyword_hit("防水鞋", "建议穿雨鞋") is False


def test_keyword_hit_supports_alternatives():
    alternatives = ["防水鞋", "雨鞋", "防水材质"]

    assert keyword_hit(alternatives, "建议穿雨鞋") is True
    assert keyword_hit(alternatives, "选防水材质的鞋") is True
    assert keyword_hit(alternatives, "穿运动鞋就行") is False


def test_keyword_label_renders_alternatives():
    assert keyword_label("晾干") == "晾干"
    assert keyword_label(["晾干", "干透"]) == "晾干/干透"


def test_normalize_removes_whitespace():
    assert normalize("每次 使用\n后\t都要清洗") == "每次使用后都要清洗"


def test_refusal_markers_cover_real_wording():
    """模型换一种说法拒绝时也要判对，否则会把正确的拒答记成失败。"""
    examples = [
        "这个问题不在我服务的公司内部制度、流程、产品范围内，无法为您提供具体指导。",
        "这个问题涉及个人护照办理，不在公司业务、制度、流程或产品的服务范围内。",
        "这不在我的服务范围内。",
        "知识库里没有查到相关内容。",
        "这些资料仅员工可见，当前账号没有查看权限。",
    ]
    for answer in examples:
        assert is_refusal(answer), answer

    # 反过来，正常回答不能被误判成拒答
    assert not is_refusal("15 度建议穿毛衣加外套")
