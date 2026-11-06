"""智能客服可以调用的工具。

search_knowledge：检索知识库，回答产品相关问题时必须先用它查资料。
get_weather：查实时天气，静态知识库回答不了这类实时数据。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.tools import tool

import config_data as config
import database
from database import (
    GENERIC_MODEL, NO_EXPIRY_DATE, ROLE_NEWCOMER, create_handoff_ticket, normalize_scope,
)
from file_history_store import load_session_scope, save_session_scope
from permissions import can_view
from sources.dingtalk_files import get_dingtalk_file_provider

USER_AGENT = "rag-customer-service/1.0"

# Open-Meteo 用的是 WMO 天气代码
WEATHER_CODES = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻毛毛雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


class ToolError(RuntimeError):
    """工具执行失败，错误信息会作为工具结果返回给模型。"""


def validity_note(chunk) -> str:
    """资料时效说明。

    只有真正设了失效期的资料才附这句话，长期有效的资料不加噪声；
    附上之后模型才知道回答制度类问题时要提醒用户资料的有效期。
    """
    expires = chunk.expires_at or ""
    if not expires or expires == NO_EXPIRY_DATE:
        return ""
    effective = chunk.effective_from or "未标注"
    return f"（资料时效：{effective} 起生效，{expires} 失效，回答时要说明这个有效期）"


def _http_json(url: str, timeout: int) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def fetch_weather(city: str) -> str:
    """查实时天气与今明两天预报，返回给模型阅读的简短文本。"""
    city = (city or "").strip()
    if not city:
        raise ToolError("城市名不能为空")

    geo = _http_json(
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": city, "count": 1, "language": "zh", "format": "json"}),
        timeout=20,
    )
    results = geo.get("results") or []
    if not results:
        raise ToolError(f"没有找到城市「{city}」，请换一个写法")

    place = results[0]
    forecast = _http_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": 2,
        }),
        timeout=20,
    )

    current = forecast["current"]
    daily = forecast["daily"]
    lines = [
        f"{place['name']}实时天气（{current['time']}）",
        f"现在：{current['temperature_2m']}℃，体感 {current['apparent_temperature']}℃，"
        f"{WEATHER_CODES.get(current['weather_code'], '未知')}，"
        f"湿度 {current['relative_humidity_2m']}%，风速 {current['wind_speed_10m']} km/h",
    ]
    for index, day in enumerate(daily["time"][:2]):
        label = "今天" if index == 0 else "明天"
        lines.append(
            f"{label}（{day}）：{daily['temperature_2m_min'][index]}~{daily['temperature_2m_max'][index]}℃，"
            f"{WEATHER_CODES.get(daily['weather_code'][index], '未知')}，"
            f"降水概率 {daily['precipitation_probability_max'][index]}%"
        )
    return "\n".join(lines)


@tool
def get_weather(city: str) -> str:
    """查询指定城市的实时天气和今明两天预报。

    当用户明确问到某个城市的天气、气温、下雨、要不要带伞时使用。
    注意：city 必须是用户自己说出来的城市，例如「杭州」「北京朝阳」。
    如果用户没有提到城市，不要调用本工具，也不要自己猜一个城市，先反问用户是哪个城市。
    """
    try:
        return fetch_weather(city)
    except ToolError as exc:
        return f"天气查询失败：{exc}。请如实告诉用户暂时查不到天气，不要编造。"


def build_knowledge_tool(vector_service, collected_sources: list[dict], session_id: str,
                         role: str = ROLE_NEWCOMER, department: str = ""):
    """按请求创建检索工具：把检索到的来源记进 collected_sources，供前端展示。

    适用范围支持会话记忆：用户提过一次型号，后续追问会自动沿用。
    检索时会按 role 过滤可见性，新人拿不到仅员工可见的资料。
    """

    @tool
    def search_knowledge(query: str, scope: str = "") -> str:
        """检索客服知识库，返回最相关的资料片段。

        知识库里有产品手册、制度流程、部门资料等。
        凡是回答这两类问题（产品相关、或者按气温/天气给穿衣建议），都必须先调用它查资料，
        不要凭自己的记忆直接回答。如果第一次没查到，可以换用更具体的说法再查一次。

        query：检索用的关键词。
        scope：适用范围，用户提到的产品型号、部门或业务线，例如「X20 Pro」「人力资源部」。
        用户这轮没说就留空，系统会自动沿用本次会话里之前提到过的适用范围。
        """
        explicit = normalize_scope(scope) if str(scope).strip() else ""
        used_scope = explicit or load_session_scope(session_id)
        chunks = vector_service.search(query, used_scope or None, role, department)
        if not chunks:
            return "知识库里没有检索到相关内容。"

        if explicit and explicit != GENERIC_MODEL:
            save_session_scope(session_id, explicit)

        note = ""
        if used_scope and used_scope != GENERIC_MODEL and not any(
            used_scope.lower() in chunk.scope.lower() for chunk in chunks
        ):
            note = (
                f"注意：知识库里没有「{used_scope}」的专属资料，以下返回的是通用资料，"
                "回答时要如实说明这一点。\n\n"
            )
        elif not used_scope:
            scopes_in_result = {chunk.scope for chunk in chunks}
            if len(scopes_in_result) > 1:
                note = (
                    "注意：检索结果里包含多个型号的资料，回答时要提醒用户说明具体型号，"
                    "不要把不同型号的结论混在一起。\n\n"
                )

        for chunk in chunks:
            source = chunk.as_source()
            if source not in collected_sources:
                collected_sources.append(source)
        return note + "\n\n".join(
            f"[{index}] 来源：{chunk.source}{validity_note(chunk)}\n{chunk.content}"
            for index, chunk in enumerate(chunks, start=1)
        )

    return search_knowledge


def build_dingtalk_search_tool(role: str, collected_files: list[dict],
                               search_state: dict | None = None,
                               department: str = ""):
    """在钉盘 / 群文件里搜索文件，结果交给前端渲染成可下载的卡片。

    search_state 用来把「搜到了但没权限」告诉主链路：
    这种情况不是知识盲区（资料存在，只是当前账号看不到），不该进盲区清单。
    """

    @tool
    def search_dingtalk_files(query: str, publisher: str = "", date_from: str = "",
                              date_to: str = "") -> str:
        """在钉盘和群文件里搜索资料，返回文件名、发布人、发布时间和所在群。

        只要用户提到钉盘、群文件、找文件、或者「某某发的资料」，就必须调用本工具；
        不要用 search_knowledge 代替，也不要回答「我无法操作钉盘」。

        query：文件主题关键词，例如「中秋后战略安排」「充电说明」。
        publisher：发布人姓名，用户说「某某发的」时填，例如「张伟」。
        date_from / date_to：可选日期范围，格式 YYYY-MM-DD。
        """
        try:
            files = get_dingtalk_file_provider().search(
                str(query or ""),
                str(publisher or "").strip(),
                str(date_from or "").strip(),
                str(date_to or "").strip(),
            )
        except NotImplementedError as exc:
            return f"钉钉检索暂不可用：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具失败要让模型知道原因
            return f"钉钉检索失败：{type(exc).__name__}: {exc}"

        visible = [item for item in files
                   if can_view(item.visibility, item.department, role, department)]
        if not visible and files:
            if search_state is not None:
                search_state["permission_denied"] = True
            return (
                "搜索到了相关资料，但当前账号没有查看权限"
                "（可能仅限本部门、主管或人事）。请如实告诉用户需要相应权限，"
                "或者联系资料所有者；"
                "不要编造文件名、内容，也不要换关键词绕过权限。"
            )
        files = visible
        if not files:
            return "钉盘里没有找到匹配的文件。可以换关键词、发布人或日期范围再试。"

        for item in files:
            payload = item.as_dict()
            if payload not in collected_files:
                collected_files.append(payload)

        lines = [f"在钉盘/群文件里找到 {len(files)} 份文件："]
        for index, item in enumerate(files, start=1):
            lines.append(
                f"{index}. {item.name}｜发布人：{item.publisher}"
                f"｜发布时间：{item.published_at}｜所在群：{item.group}"
                f"｜部门：{item.department}"
            )
        lines.append("界面会显示这些文件的下载按钮；回答时简要说明找到哪些文件即可，不要编造文件名。")
        return "\n".join(lines)

    return search_dingtalk_files


def max_tool_rounds() -> int:
    return config.max_tool_rounds


def build_process_tool(role: str, collected_processes: list[dict]):
    """查公司办事流程的工具：报销、请假、IT 报修等。"""

    @tool
    def lookup_process(name: str) -> str:
        """查公司办事流程，返回步骤、入口链接和负责人。

        用户问「XX 怎么办」「XX 流程是什么」时使用，例如报销、请假、IT 报修、门禁补办。
        name 传流程名称或关键词，例如「报销」「请假」。
        """
        entry = database.find_process(name, role)
        if entry is None:
            return "没有找到对应的流程。可以换个关键词再查，或者用 escalate_to_human 转人工。"

        if entry not in collected_processes:
            collected_processes.append(entry)

        lines = [entry["title"]]
        for index, step in enumerate(entry.get("steps") or [], start=1):
            lines.append(f"{index}. {step}")
        if entry.get("links"):
            lines.append("入口：" + "、".join(entry["links"]))
        owner = database.employee_label(entry.get("owner", ""))
        if owner:
            lines.append(f"负责人：{owner}")
        return "\n".join(lines)

    return lookup_process


def build_employee_tool(role: str, collected_employees: list[dict]):
    """找负责人的工具：报销找谁、社保找谁、IT 找谁。"""

    @tool
    def find_employee(topic: str) -> str:
        """按事项或部门找负责的同事，返回姓名、部门、负责内容和联系方式。

        用户问「XX 找谁」「XX 谁负责」「XX 归哪个部门管」时使用，
        例如「报销找谁」「社保找谁」「IT 出问题找谁」。
        topic 传事项关键词或部门名，例如「报销」「人力资源部」。
        """
        matches = database.search_employees(topic, role)
        if not matches:
            return "没有找到负责这项工作的同事。可以换个关键词，或者用 escalate_to_human 转人工。"

        for item in matches:
            if item not in collected_employees:
                collected_employees.append(item)

        lines = []
        for item in matches:
            title = f"，{item['title']}" if item.get("title") else ""
            kind = item.get("employee_type") or "员工"
            lines.append(f"{item['name']}（{item['department']}{title}，{kind}）")
            if item.get("responsibilities"):
                lines.append(f"  负责：{item['responsibilities']}")
            if item.get("contact"):
                lines.append(f"  联系：{item['contact']}")
        return "\n".join(lines)

    return find_employee


def build_handoff_tool(session_id: str, state: dict):
    """转人工工具：建工单，并把工单信息记进 state 供前端展示。"""

    @tool
    def escalate_to_human(reason: str, scope: str = "") -> str:
        """把问题转给人工客服，并生成工单号。

        遇到下面这些情况就调用它：
        - 知识库里查不到答案，而且问题没法用通用建议回答
        - 用户明确要求转人工、找人工客服
        - 涉及投诉、安全问题、质量事故
        - 需要查询订单、保修状态、维修进度等系统数据

        reason：一句话说明为什么要转人工，例如「用户反馈机器有异响，知识库没有相关说明」。
        scope：用户提到的产品型号或部门，没有就留空。
        """
        model = (
            normalize_scope(scope) if str(scope).strip()
            else load_session_scope(session_id)
        )
        ticket_no, created = create_handoff_ticket(session_id, reason, model)
        state["handoff"] = {
            "ticket_no": ticket_no,
            "reason": reason,
            "scope": model or GENERIC_MODEL,
        }
        if created:
            return f"已创建工单 {ticket_no}，人工客服会尽快跟进。请在回答里把工单号告诉用户。"
        return f"本次会话已经有未关闭的工单 {ticket_no}，不用重复建单。请在回答里把工单号告诉用户。"

    return escalate_to_human
