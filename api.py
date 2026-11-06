"""RAG 项目的 HTTP API，供 Vue 前端调用。"""

import json
import os
import logging
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, unquote
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

# 在 API 入口自动读取本地 .env；如果用户通过系统环境变量配置，也同样有效。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import config_data as config
import documents
import file_history_store
from knowledge_base import KnowledgeBaseService
from rag import RagService
from auth import (
    login as auth_login, logout as auth_logout, optional_user, require_staff,
    require_user, role_of,
)
from database import (
    database_ready, delete_employee, delete_qa_turns, feedback_stats, list_audit_logs,
    list_blind_spots, list_current_filenames, list_employees, list_scopes,
    list_session_turns, log_audit,
    normalize_department, normalize_scope, normalize_visibility, search_employees, set_feedback,
    upsert_employee,
)
from sources.dingtalk_files import get_dingtalk_file_provider
from permissions import (
    VIS_ALL, VIS_DEPARTMENT, VIS_SENSITIVE, VIS_STAFF, allowed_upload_visibilities,
    can_view, department_selectable, is_hr, normalize_visibility as normalize_doc_visibility,
)

logger = logging.getLogger(__name__)


def parse_validity_date(value: str, field: str) -> str | None:
    """把上传时带的生效/失效日期解析成 ISO 日期；空值表示不限制。"""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} 需要 YYYY-MM-DD 格式的日期"
        ) from exc


def warm_up() -> None:
    """预热：向量客户端、PGEngine 的后台事件循环和连接池都是懒加载的，
    不预热的话服务刚启动后的第一次检索会明显变慢。"""
    try:
        get_rag_service().search("预热", None, role_of(None))
        logger.info("预热完成")
    except Exception:
        logger.warning("预热失败，首次检索可能会慢一点", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 放到后台线程里，不阻塞服务启动
    threading.Thread(target=warm_up, daemon=True).start()
    yield


app = FastAPI(title="公司内部助手 RAG API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    input: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="user_001", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class FeedbackRequest(BaseModel):
    turn_id: int = Field(gt=0)
    rating: int = Field(description="1=有用，-1=没用")
    comment: str = Field(default="", max_length=500)


rag_service: RagService | None = None
knowledge_base_service: KnowledgeBaseService | None = None


def get_rag_service() -> RagService:
    global rag_service
    if rag_service is None:
        rag_service = RagService()
    return rag_service


def get_knowledge_base_service() -> KnowledgeBaseService:
    global knowledge_base_service
    if knowledge_base_service is None:
        knowledge_base_service = KnowledgeBaseService()
    return knowledge_base_service


def sse_event(payload: dict | str) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health() -> JSONResponse:
    ready = database_ready()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "degraded",
                 "model_configured": model_configured(),
                 "storage_backend": "postgresql+pgvector", "database_ready": ready},
    )


@app.post("/api/auth/login")
def login(payload: dict) -> dict:
    """用户名 + 密码登录，返回 token。"""
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    result = auth_login(username, password)
    if result is None:
        log_audit("auth.login", username=username, status="failed")
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    token, user = result
    log_audit("auth.login", username=user["username"], role=user["role"], status="ok")
    return {"token": token, "user": user}


@app.post("/api/auth/logout")
def logout(request: Request, user: dict | None = Depends(optional_user)) -> dict:
    authorization = request.headers.get("authorization", "")
    _, _, token = authorization.partition(" ")
    if token.strip():
        auth_logout(token.strip())
    if user:
        log_audit("auth.logout", username=user["username"], role=user["role"])
    return {"message": "已退出登录"}


@app.get("/api/auth/me")
def me(user: dict | None = Depends(optional_user)) -> dict:
    """当前登录用户；没有登录返回 null，前端按最低权限渲染。"""
    return {"user": user}


def model_configured() -> bool:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    return bool(key and key != "your_dashscope_api_key_here")


def require_model() -> None:
    if not model_configured():
        raise HTTPException(status_code=503, detail="模型待配置，请管理员配置 DashScope API Key 后启用服务")


@app.post("/api/chat")
def chat(request: ChatRequest, user: dict | None = Depends(optional_user)) -> StreamingResponse:
    require_model()
    query = request.input.strip()
    if not query:
        raise HTTPException(status_code=400, detail="问题不能为空")
    role = role_of(user)
    username = (user or {}).get("username", "")
    if username:
        # 登录用户的会话自动认领；已经属于别人的会话一律拒绝
        if not file_history_store.claim_session(request.session_id, username):
            raise HTTPException(status_code=403, detail="这个会话属于其他账号")
    elif not file_history_store.can_access_session(request.session_id, ""):
        raise HTTPException(status_code=403, detail="请登录后继续这个话题")

    def stream() -> Iterator[str]:
        try:
            service = get_rag_service()
            for event in service.stream_events(
                query, request.session_id, role,
                username=(user or {}).get("username", ""),
                department=(user or {}).get("department", ""),
            ):
                if event["type"] == "content":
                    yield sse_event({"content": event["text"]})
                elif event["type"] == "turn":
                    # 把这轮回答的编号推给前端，之后点赞/点踩都挂在它上面
                    yield sse_event({"turn_id": event["turn_id"]})
                elif event["type"] == "sources":
                    yield sse_event({"sources": event["sources"]})
                elif event["type"] == "tool":
                    yield sse_event({"tool": {"name": event["name"],
                                              "arguments": event.get("arguments", {})}})
                elif event["type"] == "handoff":
                    yield sse_event({"handoff": event["handoff"]})
                elif event["type"] == "faq":
                    yield sse_event({"faq": event["faq"]})
                elif event["type"] == "process":
                    yield sse_event({"process": event["process"]})
                elif event["type"] == "employee":
                    yield sse_event({"employees": event["employees"]})
                elif event["type"] == "files":
                    # 钉盘文件卡片：前端渲染下载按钮
                    yield sse_event({"files": event["files"]})
            yield sse_event("[DONE]")
        except Exception:
            logger.exception("Chat request failed")
            yield sse_event({"error": "问答服务暂时不可用，请稍后重试"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/sessions")
def list_sessions(user: dict | None = Depends(optional_user)) -> dict:
    """只列出当前账号自己的会话；未登录不返回历史。"""
    username = (user or {}).get("username", "")
    return {"sessions": file_history_store.list_sessions(username)}


@app.get("/api/scopes")
def get_scopes() -> dict:
    """知识库里出现过的适用范围（型号 / 部门 / 业务线），供前端做下拉建议。"""
    return {"scopes": list_scopes()}


@app.get("/api/employees")
def get_employees(user: dict | None = Depends(optional_user),
                  topic: str | None = None,
                  department: str | None = None) -> dict:
    """员工通讯录：支持按事项关键词搜索（topic）和按部门筛选（department）。

    返回的内容已经按当前角色过滤过可见性，新人拿不到仅员工可见的卡片。
    """
    role = role_of(user)
    employees = search_employees(topic, role) if topic else list_employees(role=role,
                                                                          department=department)
    departments = sorted({item["department"] for item in list_employees(role=role)})
    return {"employees": employees, "departments": departments}


@app.post("/api/employees")
def create_employee(payload: dict, user: dict = Depends(require_staff)) -> dict:
    """员工新增一张通讯录卡片。编号由服务端生成，避免和现有人撞号。"""
    name = str(payload.get("name", "")).strip()
    department = str(payload.get("department", "")).strip()
    if not name or not department:
        raise HTTPException(status_code=400, detail="姓名和部门不能为空")

    employee = {
        "employee_id": f"emp-{uuid4().hex[:8]}",
        "name": name[:50],
        "department": department[:50],
        "title": str(payload.get("title", "")).strip()[:50],
        "employee_type": "主管" if str(payload.get("employee_type", "")).strip() == "主管"
        else "员工",
        "responsibilities": str(payload.get("responsibilities", "")).strip()[:300],
        "contact": str(payload.get("contact", "")).strip()[:100],
        "keywords": str(payload.get("keywords", "")).strip()[:200],
        "scope": normalize_scope(payload.get("scope")),
        "visibility": normalize_visibility(payload.get("visibility")),
    }
    upsert_employee(employee)
    log_audit("employee.create", username=user["username"], role=user["role"],
              target=employee["employee_id"],
              detail={"name": employee["name"], "department": employee["department"],
                      "visibility": employee["visibility"]})
    return {"employee": employee}


@app.delete("/api/employees/{employee_id}")
def remove_employee(employee_id: str, user: dict = Depends(require_staff)) -> dict:
    """员工删除一张通讯录卡片；仍是流程负责人的会返回提示，便于重新指派。"""
    removed = delete_employee(employee_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="没有找到这张员工卡片")
    log_audit("employee.delete", username=user["username"], role=user["role"],
              target=employee_id,
              detail={"name": removed["name"], "department": removed["department"],
                      "referenced_by": removed["referenced_by"]})
    return {"message": f"已删除 {removed['name']}",
            "referenced_by": removed["referenced_by"]}


@app.get("/api/dingtalk/files/{file_id}/download")
def download_dingtalk_file(file_id: str,
                           user: dict = Depends(require_user)) -> Response:
    """下载钉盘文件。mock 模式直接返回本地文件，real 模式会转而调用钉钉下载接口。"""
    provider = get_dingtalk_file_provider()
    item = provider.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到这个文件")
    if not can_view(item.visibility, item.department, role_of(user),
                    user.get("department", ""), include_sensitive=True):
        raise HTTPException(status_code=403, detail="你没有权限下载这个文件")
    try:
        content, filename = provider.read(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件内容不存在") from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    log_audit("dingtalk.download", username=user["username"], role=user["role"],
              target=filename, detail={"file_id": file_id})
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/files")
def list_available_files(user: dict = Depends(require_user)) -> dict:
    """资料文件库：当前账号能看到的所有模拟文件（敏感资料只给人事）。"""
    provider = get_dingtalk_file_provider()
    role = role_of(user)
    department = user.get("department", "")
    imported = list_current_filenames()
    files = []
    for item in provider.list_all():
        if not can_view(item.visibility, item.department, role, department,
                        include_sensitive=is_hr(role, department)):
            continue
        payload = item.as_dict()
        payload["sensitive"] = item.visibility == VIS_SENSITIVE
        payload["can_import"] = item.visibility != VIS_SENSITIVE
        payload["imported"] = item.name in imported
        files.append(payload)
    return {"files": files}


@app.post("/api/files/{file_id}/import")
def import_available_file(file_id: str,
                          user: dict = Depends(require_user)) -> dict:
    """把资料文件导入知识库；部门归属按当前账号权限收紧。"""
    provider = get_dingtalk_file_provider()
    role = role_of(user)
    my_department = normalize_department(user.get("department", ""))
    if my_department == "通用":
        raise HTTPException(status_code=400, detail="当前账号没有配置部门，请联系管理员")

    item = provider.get(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到这个文件")
    if item.visibility == VIS_SENSITIVE:
        raise HTTPException(status_code=403, detail="敏感资料不允许导入问答知识库")
    if not can_view(item.visibility, item.department, role, my_department):
        raise HTTPException(status_code=403, detail="你没有权限导入这个文件")

    try:
        content, filename = provider.read(file_id)
        text = documents.extract_text(content, filename)
    except (documents.UnsupportedFileType, documents.ExtractionFailed) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件内容不存在") from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")

    require_model()
    department = normalize_department(item.department)
    if not is_hr(role, my_department) and department not in (my_department, "通用"):
        # 公开文件可以导入，但归属落到自己的部门，避免新人把资料挂到别的部门
        department = my_department
    visibility = item.visibility if item.visibility in (
        VIS_ALL, VIS_DEPARTMENT, VIS_STAFF) else VIS_ALL
    if visibility == VIS_DEPARTMENT and department == "通用":
        visibility = VIS_ALL

    message = get_knowledge_base_service().upload(
        text, item.name, scope=department, visibility=visibility,
        source_type="upload", source_key=None,
        department=department,
    )
    log_audit("file.import", username=user["username"], role=user["role"],
              target=item.name,
              detail={"file_id": file_id, "department": department,
                      "visibility": visibility, "result": message})
    return {"message": message, "department": department,
            "visibility": visibility, "imported": True}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, user: dict | None = Depends(optional_user)) -> dict:
    """读取某个会话的全部消息，用于回看和继续话题。"""
    if not file_history_store.is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="会话 ID 不合法")
    if not file_history_store.can_access_session(session_id, (user or {}).get("username", "")):
        raise HTTPException(status_code=403, detail="这个会话属于其他账号")
    messages = file_history_store.load_messages(session_id)
    summary = file_history_store.load_summary(session_id).get("summary", "")
    turns = list_session_turns(session_id)

    def turn_of(message, used: set[int]) -> dict | None:
        """把助手回答和历史轮次对上号：只有正文完全一致才认，避免张冠李戴。"""
        for index, turn in enumerate(turns):
            if index in used or turn["answer"] != message.content:
                continue
            used.add(index)
            return turn
        return None

    used_turns: set[int] = set()
    history = []
    for message in messages:
        item = {
            "role": "user" if message.type == "human" else "assistant",
            "content": message.content,
        }
        if item["role"] == "assistant":
            turn = turn_of(message, used_turns)
            if turn is not None:
                item["turn_id"] = turn["id"]
                item["feedback"] = turn["feedback"]
                item["evidence"] = turn["evidence"]
                item["files"] = turn.get("files") or []
        history.append(item)
    return {
        "id": session_id,
        "summary": summary,
        "messages": history,
    }


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str,
                   user: dict | None = Depends(optional_user)) -> dict:
    if not file_history_store.is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="会话 ID 不合法")
    if not file_history_store.can_access_session(session_id, (user or {}).get("username", "")):
        raise HTTPException(status_code=403, detail="这个会话属于其他账号")
    if not file_history_store.delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    # 聊天文件删了，问答留痕（含问题和回答原文）也要一起删
    removed_turns = delete_qa_turns(session_id, (user or {}).get("username", ""))
    log_audit("session.delete", username=(user or {}).get("username", ""),
              role=role_of(user), target=session_id,
              detail={"deleted_turns": removed_turns})
    return {"message": "会话已删除"}


@app.post("/api/feedback")
def submit_feedback(request: FeedbackRequest,
                    user: dict | None = Depends(optional_user)) -> dict:
    """给某轮回答打分。任意角色都能反馈，包括未登录的新人。"""
    if request.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="评价只能是 1（有用）或 -1（没用）")
    if not set_feedback(request.turn_id, request.rating, request.comment):
        raise HTTPException(status_code=404, detail="找不到这一轮回答")
    log_audit("feedback.submit", username=(user or {}).get("username", ""),
              role=role_of(user), target=str(request.turn_id),
              detail={"rating": request.rating, "comment": request.comment[:200]})
    return {"message": "反馈已记录"}


@app.get("/api/reviews/blind-spots")
def get_blind_spots(days: int = 30, limit: int = 50,
                    user: dict = Depends(require_staff)) -> dict:
    """知识盲区清单：答不上来的、转人工的、被点「没用」的问题。"""
    return {"days": days, "blind_spots": list_blind_spots(days=days, limit=limit)}


@app.get("/api/reviews/stats")
def get_review_stats(days: int = 30, user: dict = Depends(require_staff)) -> dict:
    """满意度概览，给知识库维护提供一个量化依据。"""
    return feedback_stats(days=days)


@app.get("/api/audit")
def get_audit_logs(limit: int = 100, action: str = "", username: str = "",
                   user: dict = Depends(require_staff)) -> dict:
    """审计日志：谁在什么时候动了什么。"""
    return {"logs": list_audit_logs(limit=limit, action=action or None,
                                    username=username or None)}


@app.post("/api/knowledge/upload")
async def upload_knowledge(request: Request,
                           user: dict = Depends(require_user)) -> dict[str, str]:
    """接收原始文件流；所有登录用户都可以导入资料。

    支持 TXT / Markdown / PDF / Word(.docx) / 图片；图片走 OCR 识别文字。
    文件名放 `X-Filename`，适用范围放 `X-Scope`，
    归属部门放 `X-Department`（新人/主管只能填本部门或「通用」，人事可任选），
    可见性放 `X-Visibility`：all=全员 / department=本部门 / staff=仅主管 / sensitive=仅人事。
    时效放 `X-Effective-From` / `X-Expires-At`（留空=今天生效、长期有效）。
    """
    filename = unquote(request.headers.get("x-filename", ""))
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    role = user["role"]
    my_department = normalize_department(user.get("department", ""))
    if my_department == "通用":
        raise HTTPException(status_code=400, detail="当前账号没有配置部门，请联系管理员")

    scope = normalize_scope(unquote(request.headers.get("x-scope", "")))
    requested_department = unquote(request.headers.get("x-department", "")).strip()
    target_department = normalize_department(requested_department or my_department)
    if not is_hr(role, my_department) and target_department not in (my_department, "通用"):
        raise HTTPException(status_code=403, detail="只能导入本部门或通用资料")

    visibility = normalize_doc_visibility(
        request.headers.get("x-visibility", VIS_DEPARTMENT))
    if visibility not in allowed_upload_visibilities(role, my_department):
        raise HTTPException(status_code=403, detail="当前账号不能设置这个可见性")
    if visibility == VIS_DEPARTMENT and target_department == "通用":
        raise HTTPException(status_code=400, detail="本部门可见的资料必须指定部门")

    effective_from = parse_validity_date(
        request.headers.get("x-effective-from", ""), "生效日期")
    expires_at = parse_validity_date(
        request.headers.get("x-expires-at", ""), "失效日期")
    if effective_from and expires_at and expires_at < effective_from:
        raise HTTPException(status_code=400, detail="失效日期不能早于生效日期")

    suffix = documents.suffix_of(filename)
    if suffix not in documents.SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"暂不支持 {suffix or '该类型'} 文件，支持：{documents.supported_hint()}",
        )

    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容不能为空")
    if len(content) > config.max_upload_bytes:
        limit_mb = config.max_upload_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件超过 {limit_mb}MB 上限")

    require_model()

    try:
        text = await run_in_threadpool(documents.extract_text, content, filename)
    except (documents.UnsupportedFileType, documents.ExtractionFailed) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Knowledge extraction failed")
        raise HTTPException(status_code=500, detail="文件解析失败，请稍后重试") from exc

    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容不能为空")

    try:
        message = await run_in_threadpool(
            get_knowledge_base_service().upload, text, filename, scope, visibility,
            "upload", None, effective_from, expires_at,
            target_department,
        )
        log_audit(
            "knowledge.upload", username=user["username"], role=user["role"],
            target=filename,
            detail={"scope": scope, "visibility": visibility,
                    "department": target_department,
                    "effective_from": effective_from or date.today().isoformat(),
                    "expires_at": expires_at or "9999-12-31",
                    "result": message},
        )
        return {"message": message, "scope": scope, "visibility": visibility,
                "department": target_department,
                "effective_from": effective_from or date.today().isoformat(),
                "expires_at": expires_at or "9999-12-31"}
    except Exception as exc:
        logger.exception("Knowledge upload failed")
        log_audit("knowledge.upload", username=user["username"], role=user["role"],
                  target=filename,
                  detail={"scope": scope, "department": target_department},
                  status="failed")
        raise HTTPException(status_code=500, detail="知识库更新失败，请稍后重试") from exc
