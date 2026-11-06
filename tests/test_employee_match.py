"""员工卡片匹配的单元测试（纯函数，不访问数据库）。"""

from database import match_employees


def person(employee_id: str, name: str, department: str, keywords: str) -> dict:
    return {"employee_id": employee_id, "name": name, "department": department,
            "keywords": keywords, "title": "", "responsibilities": "", "contact": ""}


PEOPLE = [
    person("emp-005", "刘洋", "财务部", "报销|差旅|发票"),
    person("emp-002", "李敏", "人力资源部", "入职|社保|公积金"),
    person("emp-010", "郑凯", "信息技术部", "IT|电脑|账号"),
]


def test_matches_by_topic_keyword():
    result = match_employees("报销找谁", PEOPLE)
    assert [item["name"] for item in result] == ["刘洋"]


def test_matches_by_department_name():
    result = match_employees("人力资源部谁负责社保", PEOPLE)
    assert result[0]["name"] == "李敏"


def test_name_query_ranks_first():
    result = match_employees("找郑凯", PEOPLE)
    assert result[0]["name"] == "郑凯"


def test_no_match_returns_empty():
    assert match_employees("食堂几点开饭", PEOPLE) == []
    assert match_employees("", PEOPLE) == []


def test_limit_is_respected():
    result = match_employees("报销 社保 IT", PEOPLE, limit=2)
    assert len(result) == 2
