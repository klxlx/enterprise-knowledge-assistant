"""智能客服的问答链路。

主链路是「工具调用」：模型自己决定要不要查知识库、要不要查天气，可以多轮调用后再作答。
另外保留一条「直连检索」链路，用同一套评测集对比两种方式的效果。
两条链路的回答都流式输出，正文里的【编号】由代码统一摘掉。
"""

import logging
import re
from typing import Iterator

from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

import config_data as config
import database
from database import ROLE_NEWCOMER
from file_history_store import get_history
from history_summary import prepare_history
from tools import (
    build_dingtalk_search_tool, build_employee_tool, build_handoff_tool, build_knowledge_tool,
    build_process_tool, get_weather, validity_note,
)
from vector_stores import RetrievedChunk, VectorStoreService

logger = logging.getLogger(__name__)

# 主链路：模型可以调用工具
TOOL_SYSTEM_PROMPT = (
    "你是公司内部助手，服务对象是新人和在职员工。\n\n"
    "你可以调用下面的工具：\n"
    "- search_knowledge：检索公司知识库（制度流程、部门资料、产品手册、通用指引）。\n"
    "- get_weather：查指定城市的实时天气。\n"
    "- escalate_to_human：转人工，会生成工单号。\n"
    "- lookup_process：查公司办事流程（报销、请假、IT 报修等），返回步骤和入口链接。\n"
    "- find_employee：按事项找负责的同事，返回姓名、部门、负责内容和联系方式。\n\n"
    "- search_dingtalk_files：在钉盘 / 群文件里搜索资料，返回文件名、发布人、发布时间和所在群。\n\n"
    "工具使用规则：\n"
    "- 只要问题可能与知识库内容有关（公司制度、流程、产品、部门职责，以及按天气/气温给出的"
    "穿衣出行建议），就必须先调用 search_knowledge 查资料，不要凭记忆回答；"
    "拿不准要不要查时，也先查一次。例如「早晚温差大怎么穿」「下雨天穿什么鞋」"
    "这类问题也要先查。第一次没查到可以换个说法再查。\n"
    "- 用户提到适用对象（产品型号、部门、业务线，例如「X20 Pro」「人力资源部」）时，"
    "把它填进 search_knowledge 的 scope 参数；用户这轮没提但之前提过，可以不填，"
    "系统会自动沿用会话里记住的适用范围。\n"
    "- 如果知识库里没有该范围的专属资料，要如实说明，不要用其他范围的结论顶替。\n"
    "- 只有需要当地实时天气才能回答的问题（例如「今天天气怎么样」「今天要不要带伞」），"
    "并且用户已经说出城市时，才调用 get_weather。\n"
    "- 用户没说城市、但问题确实需要实时天气时，先反问用户在哪个城市，不要自己猜一个城市。\n"
    "- 知识库查不到、用户明确要求人工、涉及投诉或安全问题时，调用 escalate_to_human，"
    "并把返回的工单号如实告诉用户；不要自己编造工单号。\n\n"
    "- 用户问「XX 怎么办」「XX 流程」时，先调用 lookup_process 拿真实步骤，"
    "不要自己编流程或链接。\n\n"
    "- 用户问「XX 找谁」「XX 谁负责」时，调用 find_employee，"
    "把返回的姓名、部门和联系方式如实转述，不要自己编。\n\n"
    "- 用户要在钉盘 / 群里找文件、或者问「某某发的资料」时，必须调用 search_dingtalk_files；"
    "例如「帮我在钉盘上搜索各部门中秋后战略安排」「张伟在群里发的资料」。"
    "提到人名就填 publisher，提到时间范围就填 date_from / date_to。"
    "它和 search_knowledge 分工不同：知识库用来回答问题，钉盘用来找原始文件；"
    "不要回答「我不能操作钉盘」，这个工具就是用来查钉盘的。"
    "如果工具返回「仅员工可见 / 没有查看权限」，如实说明需要员工账号或联系资料所有者，"
    "不要换关键词反复尝试绕过权限。\n"
    "- 文件列表由界面展示下载按钮，回答里说明找到几份、分别是什么即可，不要编造文件名或下载链接。\n\n"
    "服务范围：只回答与公司业务、制度、流程、产品相关的问题，在钉盘/群文件里查找公司资料，"
    "以及按天气/气温给出的出行建议。\n"
    "遇到完全无关的问题（例如办证件、股票、写代码、时事新闻），礼貌说明这不在服务范围内，不要作答。\n\n"
    "回答要求：\n"
    "1. 简洁、专业，一般控制在三句话以内；除非用户要求列举，否则不要分点罗列。\n"
    "2. 资料里没有写的内容，可以按常识给出一般性建议，但不要编造具体的时间、型号、价格或参数。\n"
    "3. 不要在回答里写【1】这类引用编号，来源由界面单独展示。\n"
    "4. 需要调用工具时直接调用，不要在调用前先说「我马上帮您查」这类话；"
    "拿到工具结果后再一次性作答，不要先说一半、拿到结果再说一遍。\n"
    "5. 用纯文本回答：不要使用 ** 加粗、# 标题、表格或 Markdown 链接，界面不渲染这些标记。\n"
    "6. 资料标注了有效期时（例如「2026-12-31 失效」），回答制度类问题要顺带说明资料时效；"
    "如果资料已经过了有效期就说明它不再适用，并提示用户确认最新版本，不要拿过期标准当现行规定。\n"
    "{summary_block}"
)

# 对比用：不用工具，直接把检索到的资料塞进提示词
DIRECT_SYSTEM_PROMPT = (
    "你是智能客服。请参考下面的资料回答用户问题。\n\n"
    "回答要求：\n"
    "1. 简洁、专业，一般控制在三句话以内；除非用户要求列举，否则不要分点罗列。\n"
    "2. 资料里没有写的，可以按常识给出一般性建议，但不要编造具体的时间、型号、价格或参数；"
    "如果问题跟资料完全无关，直接说明这不在知识库范围内。\n"
    "3. 不要在回答里写【1】这类引用编号，来源由界面单独展示。\n\n"
    "参考资料：\n{context}\n"
    "{summary_block}"
)

_CITATION = re.compile(r"【\d+】")
# 【编号】最长也就几个字符，超出这个长度的"半个标记"不再当成标记处理。
_MARKER_HOLD_CHARS = 8
# 每轮回答开头先压住这么多字：如果这轮其实是要调工具，模型常会先说一句
# "我马上帮您查"之类的过场话，压住就能丢掉；超过这个长度说明是真回答，正常放行。
_PREAMBLE_HOLD_CHARS = 60


def _split_for_stream(text: str, size: int = 12):
    """把一段固定文本切小块，保持前端逐字出现的手感。"""
    for start in range(0, len(text), size):
        yield text[start:start + size]


def _record_turn(session_id: str, username: str, role: str, question: str,
                 answer: str, evidence: str, source_count: int = 0,
                 tool_count: int = 0, handoff: bool = False,
                 files: list[dict] | None = None) -> dict:
    """给这一轮问答留痕，返回要推给前端的 turn 事件。

    evidence 记录这轮回答的依据来自哪里（faq / knowledge / process / employee /
    weather / handoff），空字符串代表"什么都没用上"——盲区清单主要看这一类。
    评测会话（eval-*）不记账；留痕失败也不能影响用户已经看到的回答。
    """
    if session_id.startswith("eval-"):
        return {"type": "turn", "turn_id": None}
    try:
        turn_id = database.record_qa_turn(
            session_id, question, answer, evidence,
            source_count=source_count, tool_count=tool_count,
            handoff=handoff, username=username, role=role, files=files or [],
        )
    except Exception:  # noqa: BLE001 - 留痕是旁路，失败也不能让回答报错
        logger.warning("问答留痕失败", exc_info=True)
        return {"type": "turn", "turn_id": None}
    return {"type": "turn", "turn_id": turn_id}


def _split_partial_marker(text: str) -> tuple[str, str]:
    """把末尾可能是半个【编号】的部分留到下一块，避免标记被切成两半。"""
    start = text.rfind("【")
    if start == -1 or "】" in text[start:]:
        return text, ""
    if len(text) - start <= _MARKER_HOLD_CHARS:
        return text[:start], text[start:]
    return text, ""


def _strip_citations(text: str, citations: list[str]) -> str:
    """摘掉正文里的【编号】，并按出现顺序记录下来。"""
    for match in _CITATION.finditer(text):
        if match.group(0) not in citations:
            citations.append(match.group(0))
    return _CITATION.sub("", text)


class _StreamCollector:
    """边接收模型流式片段边清洗正文，同时收集工具调用。"""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.citations: list[str] = []
        self.tool_call_message = None
        self._pending = ""
        self._hold = ""
        self._committed = False

    @property
    def has_tool_call(self) -> bool:
        return self.tool_call_message is not None

    def feed(self, piece) -> str:
        if getattr(piece, "tool_call_chunks", None):
            self.tool_call_message = (
                piece if self.tool_call_message is None else self.tool_call_message + piece
            )
            # 这轮确定是工具轮，压住的过场话直接丢掉
            self._hold = ""
        content = piece.content if isinstance(piece.content, str) else str(piece.content or "")
        if not content:
            return ""
        self._pending += content
        safe, self._pending = _split_partial_marker(self._pending)
        if not safe:
            return ""
        cleaned = _strip_citations(safe, self.citations)
        if not cleaned:
            return ""

        if self._committed:
            self.text_parts.append(cleaned)
            return cleaned

        self._hold += cleaned
        if len(self._hold) < _PREAMBLE_HOLD_CHARS:
            return ""
        self._committed = True
        released, self._hold = self._hold, ""
        self.text_parts.append(released)
        return released

    def flush(self) -> str:
        cleaned = _strip_citations(self._pending, self.citations) if self._pending else ""
        self._pending = ""
        if cleaned:
            self._hold += cleaned
        if self.has_tool_call:
            # 工具轮的过场话不要
            self._hold = ""
            return ""
        if not self._hold:
            return ""
        released, self._hold = self._hold, ""
        self._committed = True
        self.text_parts.append(released)
        return released

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def tool_calls(self) -> list[dict]:
        if self.tool_call_message is None:
            return []
        return list(self.tool_call_message.tool_calls)


class RagService(object):

    def __init__(self, embedding=None):
        self.vector_service = VectorStoreService(
            embedding=embedding if embedding is not None else DashScopeEmbeddings(
                model=config.embedding_model_name
            ),
        )

        self.tool_prompt = ChatPromptTemplate.from_messages([
            ("system", TOOL_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("user", "用户提问：{input}"),
        ])
        self.direct_prompt = ChatPromptTemplate.from_messages([
            ("system", DIRECT_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("user", "用户提问：{input}"),
        ])

        # streaming=True 是必须的：默认情况下 LangChain 认为该模型不支持流式，
        # 会把整段回答一次性返回，前端就只能看到"瞬间出现"。
        self.chat_model = ChatTongyi(model=config.chat_model_name, streaming=True)
        self.model_extras = (
            {"enable_thinking": False} if config.disable_thinking else {}
        )

    def search(self, question: str, scope: str | None = None,
               role: str = ROLE_NEWCOMER, department: str = "") -> list[RetrievedChunk]:
        return self.vector_service.search(question, scope, role, department)

    @staticmethod
    def format_context(chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return "无"
        return "\n\n".join(
            f"【{chunk.index}】来源：{chunk.source}{validity_note(chunk)}\n{chunk.content}"
            for chunk in chunks
        )

    @staticmethod
    def _summary_block(summary_text: str) -> str:
        if not summary_text:
            return ""
        return f"\n先前对话摘要（早于下面几轮对话）：\n{summary_text}\n"

    def stream_events(self, question: str, session_id: str,
                      role: str = ROLE_NEWCOMER,
                      username: str = "", department: str = "") -> Iterator[dict]:
        """带工具的问答主链路，产出 sources / tool / handoff / content 事件。

        role 决定能看到哪些资料：新人只能看全员可见的，员工还能看内部资料。
        每轮结束会把问答留痕写进 qa_turns，并推一个 turn 事件给前端挂反馈按钮；
        评测用的 eval-* 会话不记账，避免把评测题目算进知识盲区。
        """
        history = get_history(session_id)

        # FAQ 直连：常见问题命中关键词就直接返回，不调用模型（秒级）
        faq = database.find_faq(question, role)
        if faq:
            answer_text = faq.get("answer") or faq.get("title", "")
            yield {"type": "faq",
                   "faq": {"title": faq["title"], "question": faq["question"]}}
            for piece in _split_for_stream(answer_text):
                yield {"type": "content", "text": piece}
            history.add_messages([
                HumanMessage(content=question),
                AIMessage(content=answer_text),
            ])
            yield _record_turn(session_id, username, role, question, answer_text,
                               "faq")
            return

        summary_text, recent = prepare_history(session_id, list(history.messages))

        collected_sources: list[dict] = []
        handoff_state: dict = {}
        collected_processes: list[dict] = []
        collected_employees: list[dict] = []
        collected_files: list[dict] = []
        dingtalk_state: dict = {}
        knowledge_tool = build_knowledge_tool(
            self.vector_service, collected_sources, session_id, role, department
        )
        handoff_tool = build_handoff_tool(session_id, handoff_state)
        process_tool = build_process_tool(role, collected_processes)
        employee_tool = build_employee_tool(role, collected_employees)
        dingtalk_tool = build_dingtalk_search_tool(
            role, collected_files, dingtalk_state, department
        )
        tools = (knowledge_tool, get_weather, handoff_tool, process_tool, employee_tool,
                 dingtalk_tool)
        tool_map = {item.name: item for item in tools}
        model = self.chat_model.bind_tools(list(tools))

        messages = self.tool_prompt.format_messages(
            summary_block=self._summary_block(summary_text),
            history=recent,
            input=question,
        )

        answer_parts: list[str] = []
        called_tools: list[str] = []
        rounds = 0
        while True:
            collector = _StreamCollector()
            for piece in model.stream(messages, **self.model_extras):
                text = collector.feed(piece)
                if text:
                    answer_parts.append(text)
                    yield {"type": "content", "text": text}
            tail = collector.flush()
            if tail:
                answer_parts.append(tail)
                yield {"type": "content", "text": tail}

            tool_calls = collector.tool_calls
            if not tool_calls:
                break

            rounds += 1
            if rounds > config.max_tool_rounds:
                # 达到上限后不再执行工具，要求模型基于已有信息作答
                messages.append(collector.tool_call_message)
                messages.append(HumanMessage(
                    content="请基于已经获得的信息直接给出最终回答，不要再调用工具。"
                ))
                continue

            messages.append(collector.tool_call_message)
            for call in tool_calls:
                called_tools.append(call["name"])
                tool = tool_map.get(call["name"])
                yield {"type": "tool", "name": call["name"], "arguments": dict(call["args"])}
                if tool is None:
                    result = f"没有名为 {call['name']} 的工具。"
                else:
                    try:
                        result = tool.invoke(call["args"])
                    except Exception as exc:  # 工具失败也要让模型知道，别让它瞎猜
                        result = f"工具执行失败：{exc}"
                messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

            if collected_sources:
                yield {"type": "sources", "sources": list(collected_sources)}
            if handoff_state.get("handoff"):
                yield {"type": "handoff", "handoff": handoff_state["handoff"]}
            if collected_processes:
                entry = collected_processes[-1]
                yield {"type": "process", "process": {
                    "title": entry["title"],
                    "steps": entry.get("steps") or [],
                    "links": entry.get("links") or [],
                    "owner": database.employee_label(entry.get("owner", "")),
                }}
            if collected_employees:
                yield {"type": "employee", "employees": [
                    {
                        "name": item["name"],
                        "department": item["department"],
                        "title": item.get("title", ""),
                        "employee_type": item.get("employee_type", "员工"),
                        "responsibilities": item.get("responsibilities", ""),
                        "contact": item.get("contact", ""),
                    }
                    for item in collected_employees
                ]}
            if collected_files:
                yield {"type": "files", "files": list(collected_files)}

        final_answer = "".join(answer_parts).strip()
        if final_answer:
            history.add_messages([
                HumanMessage(content=question),
                AIMessage(content=final_answer),
            ])
        handoff_done = bool(handoff_state.get("handoff"))
        evidence = []
        if collected_sources:
            evidence.append("knowledge")
        if collected_processes:
            evidence.append("process")
        if collected_employees:
            evidence.append("employee")
        if collected_files:
            evidence.append("dingtalk")
        if dingtalk_state.get("permission_denied"):
            evidence.append("permission_denied")
        if "get_weather" in called_tools:
            evidence.append("weather")
        if handoff_done:
            evidence.append("handoff")
            database.log_audit(
                "handoff.created", username=username, role=role,
                target=handoff_state["handoff"].get("ticket_no", ""),
                detail={"question": question[:200],
                        "scope": handoff_state["handoff"].get("scope", "")},
            )
        yield _record_turn(
            session_id, username, role, question, final_answer, ",".join(evidence),
            source_count=len(collected_sources), tool_count=len(called_tools),
            handoff=handoff_done, files=collected_files,
        )

    def stream_answer(self, question: str, chunks: list[RetrievedChunk], session_id: str):
        """对比用：直接用给定的检索结果生成回答，不调用工具。"""
        history = get_history(session_id)
        summary_text, recent = prepare_history(session_id, list(history.messages))

        messages = self.direct_prompt.format_messages(
            context=self.format_context(chunks),
            summary_block=self._summary_block(summary_text),
            history=recent,
            input=question,
        )

        collector = _StreamCollector()
        try:
            for piece in self.chat_model.stream(messages, **self.model_extras):
                text = collector.feed(piece)
                if text:
                    yield text
            tail = collector.flush()
            if tail:
                yield tail
        finally:
            # 即使中途断开也保留已经生成的部分，下一轮仍能接着聊。
            if collector.text.strip():
                history.add_messages([
                    HumanMessage(content=question),
                    AIMessage(content=collector.text.strip()),
                ])


if __name__ == "__main__":
    service = RagService()
    question = "杭州今天天气怎么样？要带伞吗？"
    for event in service.stream_events(question, "user_001"):
        if event["type"] == "content":
            print(event["text"], end="", flush=True)
        else:
            print(f"\n[{event['type']}: {event.get('name') or event.get('sources')}]")
