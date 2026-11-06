"""钉钉文件检索（模拟索引）的单元测试，不访问网络。"""

import pytest

from sources.dingtalk_files import (
    MockDingTalkFileProvider, RealDingTalkFileProvider, query_chunks,
)


@pytest.fixture
def provider():
    return MockDingTalkFileProvider()


def test_query_chunks_strips_conversational_phrases():
    chunks = query_chunks("帮我在钉盘上搜索关于各部门中秋后战略安排")
    assert chunks == ["各部门中秋后战略安排"]


def test_search_finds_department_strategy_files(provider):
    files = provider.search("帮我在钉盘上搜索关于各部门中秋后战略安排")

    names = [item.name for item in files]
    assert len(names) == 4
    assert all("中秋后战略安排" in name for name in names)
    assert {item.publisher for item in files} == {"王工", "周涛", "李敏", "刘洋"}
    # 最新发布的排前面
    assert files[0].publisher == "刘洋"


def test_search_filters_by_publisher(provider):
    files = provider.search("", publisher="张伟")

    assert [item.name for item in files] == ["薪资结构说明（内部）.txt"]
    # 薪资属于敏感资料，只给人事主管看，不进入问答
    assert files[0].visibility == "sensitive"


def test_search_matches_publisher_name_in_query(provider):
    """模型可能把「张伟」同时放进 query 和 publisher，文件仍要能命中。"""
    files = provider.search("张伟", publisher="张伟")

    assert [item.name for item in files] == ["薪资结构说明（内部）.txt"]


def test_search_filters_by_date_range(provider):
    files = provider.search("战略安排", date_from="2026-09-22", date_to="2026-09-23")

    assert {item.publisher for item in files} == {"周涛", "李敏"}


def test_generic_word_does_not_match_unrelated_file(provider):
    """「说明」这种通用词不能把薪资文件带出来。"""
    assert [item.name for item in provider.search("充电说明")] == ["X20 Pro 充电说明.txt"]


def test_read_returns_file_content(provider):
    item = provider.search("T30 故障")[0]

    content, filename = provider.read(item.file_id)

    assert b"T30" in content
    assert filename == item.name


def test_read_missing_file_raises(provider):
    with pytest.raises(FileNotFoundError):
        provider.read("dt-not-exist")


def test_real_provider_requires_credentials():
    provider = RealDingTalkFileProvider("", "")
    with pytest.raises(RuntimeError, match="DINGTALK_APP_KEY"):
        provider.search("战略安排")
