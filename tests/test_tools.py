"""工具模块的单元测试（HTTP 全部打桩，不访问网络）。"""

import pytest

import tools


def test_fetch_weather_formats_reading(monkeypatch):
    def fake_http_json(url, timeout):
        if "geocoding" in url:
            return {"results": [{"name": "杭州", "latitude": 30.29, "longitude": 120.16}]}
        return {
            "current": {
                "time": "2026-10-13T23:00", "temperature_2m": 21.8,
                "apparent_temperature": 22.8, "weather_code": 2,
                "relative_humidity_2m": 75, "wind_speed_10m": 10.6,
            },
            "daily": {
                "time": ["2026-10-13", "2026-10-14"],
                "temperature_2m_min": [16.9, 17.0],
                "temperature_2m_max": [28.5, 29.0],
                "precipitation_probability_max": [0, 20],
                "weather_code": [3, 61],
            },
        }

    monkeypatch.setattr(tools, "_http_json", fake_http_json)

    text = tools.fetch_weather("杭州")

    assert "杭州" in text
    assert "21.8" in text
    assert "今天" in text and "明天" in text
    assert "降水概率" in text
    assert "小雨" in text          # 天气代码 61 的中文描述


def test_unknown_city_raises_tool_error(monkeypatch):
    monkeypatch.setattr(tools, "_http_json", lambda url, timeout: {"results": []})

    with pytest.raises(tools.ToolError):
        tools.fetch_weather("不存在的城市")


def test_weather_tool_without_city_returns_message():
    # 空城市不应该抛异常，而是把失败信息交给模型去解释
    result = tools.get_weather.invoke({"city": "  "})

    assert "天气查询失败" in result
    assert "不要编造" in result


def test_dingtalk_tool_flags_permission_denied():
    """搜到了但没权限查看：要标记 permission_denied，不能当成知识盲区。"""
    files, state = [], {}
    tool = tools.build_dingtalk_search_tool("newcomer", files, state)

    result = tool.invoke({"query": "薪资结构", "publisher": "张伟"})

    assert "没有查看权限" in result
    assert state.get("permission_denied") is True
    assert files == []


def test_dingtalk_tool_returns_visible_files_without_permission_flag():
    files, state = [], {}
    tool = tools.build_dingtalk_search_tool("staff", files, state)

    result = tool.invoke({"query": "中秋后战略安排"})

    assert "找到 4 份文件" in result
    assert len(files) == 4
    assert "permission_denied" not in state
