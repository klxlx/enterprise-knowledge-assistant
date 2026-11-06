from fastapi.testclient import TestClient
from urllib.parse import quote

from api import app
import api
from auth import optional_user


client = TestClient(app)

STAFF = {"username": "alice", "display_name": "Alice", "role": "staff",
         "department": "客户服务部"}
NEWCOMER = {"username": "bob", "display_name": "Bob", "role": "newcomer",
            "department": "客户服务部"}
HR = {"username": "hr", "display_name": "李敏", "role": "staff",
      "department": "人力资源部"}


def login_as(user):
    """把鉴权依赖换成指定身份，避免测试依赖真实数据库。"""
    app.dependency_overrides[optional_user] = lambda: user


def logout():
    app.dependency_overrides.pop(optional_user, None)


def test_health_check(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(api, "database_ready", lambda: True)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model_configured": False,
                               "storage_backend": "postgresql+pgvector", "database_ready": True}


def test_health_reports_database_failure(monkeypatch):
    monkeypatch.setattr(api, "database_ready", lambda: False)
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["database_ready"] is False


def test_unconfigured_chat_returns_503(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    response = client.post("/api/chat", json={"input": "hello", "session_id": "test"})
    assert response.status_code == 503


def test_session_id_rejects_path_traversal():
    response = client.post("/api/chat", json={"input": "hello", "session_id": "../../etc/passwd"})
    assert response.status_code == 422


def test_upload_rejects_unsupported_file_types():
    login_as(STAFF)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "knowledge.xlsx"},
        content=b"not a supported file",
    )
    logout()

    assert response.status_code == 400
    assert "支持" in response.json()["detail"]


def test_upload_rejects_empty_body():
    login_as(STAFF)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "knowledge.txt"},
        content=b"",
    )
    logout()

    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def test_upload_requires_login():
    logout()
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "knowledge.txt"},
        content=b"content",
    )

    assert response.status_code == 401


def test_upload_allows_newcomer_in_own_department(monkeypatch):
    class FakeKB:
        def upload(self, *args, **kwargs):
            return "[成功]测试入库"

    monkeypatch.setattr(api, "get_knowledge_base_service", lambda: FakeKB())
    monkeypatch.setattr(api, "require_model", lambda: None)
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: None)

    login_as(NEWCOMER)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "note.txt",
                 "X-Department": quote("客户服务部"),
                 "X-Visibility": "department"},
        content=b"hello",
    )
    logout()

    assert response.status_code == 200
    assert response.json()["department"] == "客户服务部"


def test_upload_rejects_other_department_for_newcomer():
    login_as(NEWCOMER)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "note.txt", "X-Department": quote("财务部")},
        content=b"hello",
    )
    logout()

    assert response.status_code == 403
    assert "本部门或通用" in response.json()["detail"]


def test_upload_rejects_sensitive_for_newcomer():
    login_as(NEWCOMER)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "note.txt",
                 "X-Department": quote("客户服务部"),
                 "X-Visibility": "sensitive"},
        content=b"hello",
    )
    logout()

    assert response.status_code == 403
    assert "可见性" in response.json()["detail"]


def test_upload_rejects_bad_validity_date():
    login_as(STAFF)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "policy.txt", "X-Expires-At": "2026/12/31"},
        content=b"content",
    )
    logout()

    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_upload_rejects_expiry_before_effective():
    login_as(STAFF)
    response = client.post(
        "/api/knowledge/upload",
        headers={"X-Filename": "policy.txt",
                 "X-Effective-From": "2026-07-01", "X-Expires-At": "2026-01-01"},
        content=b"content",
    )
    logout()

    assert response.status_code == 400
    assert "失效日期不能早于生效日期" in response.json()["detail"]


def test_feedback_rejects_unknown_rating(monkeypatch):
    called = []
    monkeypatch.setattr(api, "set_feedback", lambda *args: called.append(args) or True)
    response = client.post("/api/feedback", json={"turn_id": 1, "rating": 0})

    assert response.status_code == 400
    assert called == []          # 参数不合法时不该写库


def test_feedback_reports_missing_turn(monkeypatch):
    monkeypatch.setattr(api, "set_feedback", lambda *args: False)
    response = client.post("/api/feedback", json={"turn_id": 999, "rating": -1})

    assert response.status_code == 404


def test_feedback_records_rating_and_audit(monkeypatch):
    recorded, audited = [], []
    monkeypatch.setattr(api, "set_feedback",
                        lambda turn_id, rating, comment: recorded.append(
                            (turn_id, rating, comment)) or True)
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: audited.append((a, kw)))

    response = client.post("/api/feedback",
                           json={"turn_id": 7, "rating": -1, "comment": "答偏了"})

    assert response.status_code == 200
    assert recorded == [(7, -1, "答偏了")]
    assert audited and audited[0][0][0] == "feedback.submit"


def test_reviews_require_staff(monkeypatch):
    monkeypatch.setattr(api, "list_blind_spots", lambda **kwargs: [])
    monkeypatch.setattr(api, "feedback_stats", lambda **kwargs: {"turns": 0})

    for path in ("/api/reviews/blind-spots", "/api/reviews/stats", "/api/audit"):
        assert client.get(path).status_code == 401
        login_as(NEWCOMER)
        assert client.get(path).status_code == 403
        logout()

    login_as(STAFF)
    assert client.get("/api/reviews/blind-spots").json() == {"days": 30, "blind_spots": []}
    assert client.get("/api/reviews/stats").json()["turns"] == 0
    logout()


def test_audit_returns_logs_for_staff(monkeypatch):
    monkeypatch.setattr(api, "list_audit_logs", lambda **kwargs: [
        {"id": 1, "action": "knowledge.upload", "username": "alice"}
    ])
    login_as(STAFF)
    response = client.get("/api/audit?limit=5")
    logout()

    assert response.status_code == 200
    assert response.json()["logs"][0]["action"] == "knowledge.upload"


class FakeMessage:
    def __init__(self, type_, content):
        self.type = type_
        self.content = content


def test_session_detail_attaches_turn_id(monkeypatch):
    """回看历史会话要能还原每轮回答的编号和评价状态，才能继续点赞点踩。"""
    monkeypatch.setattr(api.file_history_store, "is_valid_session_id", lambda _: True)
    monkeypatch.setattr(api.file_history_store, "load_messages", lambda _: [
        FakeMessage("human", "滤网可以水洗吗？"),
        FakeMessage("ai", "可以水洗，洗干净后要晾干。"),
    ])
    monkeypatch.setattr(api.file_history_store, "load_summary", lambda _: {})
    monkeypatch.setattr(api, "list_session_turns", lambda _: [
        {"id": 5, "answer": "可以水洗，洗干净后要晾干。", "feedback": -1,
         "evidence": "knowledge",
         "files": [{"file_id": "dt-1", "name": "滤网维护手册.txt"}]},
    ])

    response = client.get("/api/sessions/user_1")

    assert response.status_code == 200
    assert response.json()["messages"][1]["turn_id"] == 5
    assert response.json()["messages"][1]["feedback"] == -1
    assert response.json()["messages"][1]["files"][0]["name"] == "滤网维护手册.txt"


def test_session_detail_skips_unmatched_turn(monkeypatch):
    """正文对不上就不挂编号：宁可没有反馈按钮，也不能把评价挂到别的回答上。"""
    monkeypatch.setattr(api.file_history_store, "is_valid_session_id", lambda _: True)
    monkeypatch.setattr(api.file_history_store, "load_messages", lambda _: [
        FakeMessage("human", "问题"),
        FakeMessage("ai", "这是被改写过的历史回答"),
    ])
    monkeypatch.setattr(api.file_history_store, "load_summary", lambda _: {})
    monkeypatch.setattr(api, "list_session_turns", lambda _: [
        {"id": 9, "answer": "完全不同的回答", "feedback": 1, "evidence": ""},
    ])

    response = client.get("/api/sessions/user_1")

    assert "turn_id" not in response.json()["messages"][1]


def test_sessions_are_scoped_to_current_user(monkeypatch):
    seen = []
    monkeypatch.setattr(api.file_history_store, "list_sessions",
                        lambda username="": seen.append(username) or [])

    login_as(STAFF)
    client.get("/api/sessions")
    logout()
    login_as(NEWCOMER)
    client.get("/api/sessions")
    logout()
    client.get("/api/sessions")          # 未登录不返回历史

    assert seen == ["alice", "bob", ""]


def test_session_detail_rejects_other_account(monkeypatch):
    monkeypatch.setattr(api.file_history_store, "is_valid_session_id", lambda _: True)
    monkeypatch.setattr(api.file_history_store, "can_access_session",
                        lambda session_id, username: False)
    login_as(STAFF)

    assert client.get("/api/sessions/user_x").status_code == 403
    assert client.delete("/api/sessions/user_x").status_code == 403
    logout()


class EmptyRagService:
    def stream_events(self, *args, **kwargs):
        return iter(())


def test_chat_claims_session_for_logged_in_user(monkeypatch):
    claimed = []
    monkeypatch.setattr(api, "require_model", lambda: None)
    monkeypatch.setattr(api, "get_rag_service", lambda: EmptyRagService())
    monkeypatch.setattr(api.file_history_store, "claim_session",
                        lambda session_id, username: claimed.append(
                            (session_id, username)) or True)
    login_as(STAFF)
    response = client.post("/api/chat", json={"input": "你好", "session_id": "user_x"})
    logout()

    assert response.status_code == 200
    assert claimed == [("user_x", "alice")]


def test_chat_rejects_session_owned_by_someone_else(monkeypatch):
    monkeypatch.setattr(api, "require_model", lambda: None)
    monkeypatch.setattr(api.file_history_store, "claim_session", lambda *args: False)
    login_as(STAFF)
    response = client.post("/api/chat", json={"input": "你好", "session_id": "user_x"})
    logout()

    assert response.status_code == 403


def test_delete_session_also_removes_qa_turns(monkeypatch):
    removed = []
    monkeypatch.setattr(api.file_history_store, "is_valid_session_id", lambda _: True)
    monkeypatch.setattr(api.file_history_store, "can_access_session", lambda *args: True)
    monkeypatch.setattr(api.file_history_store, "delete_session", lambda _: True)
    monkeypatch.setattr(api, "delete_qa_turns",
                        lambda session_id, username: removed.append(
                            (session_id, username)) or 3)
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: None)

    login_as(STAFF)
    response = client.delete("/api/sessions/user_x")
    logout()

    assert response.status_code == 200
    # 删聊天文件的同时删掉问答留痕，避免会话内容残留在统计表里
    assert removed == [("user_x", "alice")]


def test_employee_create_requires_staff():
    payload = {"name": "王小明", "department": "行政部"}
    assert client.post("/api/employees", json=payload).status_code == 401
    login_as(NEWCOMER)
    assert client.post("/api/employees", json=payload).status_code == 403
    logout()


def test_employee_create_validates_required_fields():
    login_as(STAFF)
    response = client.post("/api/employees", json={"name": "", "department": "行政部"})
    logout()

    assert response.status_code == 400
    assert "姓名和部门" in response.json()["detail"]


def test_employee_create_records_audit(monkeypatch):
    saved, audited = [], []
    monkeypatch.setattr(api, "upsert_employee", lambda employee: saved.append(employee))
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: audited.append((a, kw)))

    login_as(STAFF)
    response = client.post("/api/employees", json={
        "name": "王小明", "department": "行政部", "title": "行政专员",
        "responsibilities": "办公用品", "contact": "分机 8021",
        "keywords": "办公用品|门禁", "visibility": "all",
    })
    logout()

    assert response.status_code == 200
    assert saved[0]["name"] == "王小明"
    assert saved[0]["employee_id"].startswith("emp-")
    assert audited[0][0][0] == "employee.create"
    assert audited[0][1]["target"] == saved[0]["employee_id"]


def test_employee_delete_reports_missing_and_records_audit(monkeypatch):
    monkeypatch.setattr(api, "delete_employee", lambda employee_id: None)
    login_as(STAFF)
    assert client.delete("/api/employees/emp-none").status_code == 404
    logout()

    audited = []
    monkeypatch.setattr(api, "delete_employee", lambda employee_id: {
        "name": "王小明", "department": "行政部", "referenced_by": ["报销流程"],
    })
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: audited.append((a, kw)))
    login_as(STAFF)
    response = client.delete("/api/employees/emp-1")
    logout()

    assert response.status_code == 200
    assert response.json()["referenced_by"] == ["报销流程"]
    assert audited[0][0][0] == "employee.delete"


class FakeFileProvider:
    def __init__(self, visibility="all", department="通用"):
        self.visibility = visibility
        self.department = department

    def get(self, file_id):
        from types import SimpleNamespace
        return SimpleNamespace(visibility=self.visibility, department=self.department)

    def read(self, file_id):
        return "文件内容".encode("utf-8"), "中秋战略安排.txt"


def test_dingtalk_download_requires_login():
    logout()
    response = client.get("/api/dingtalk/files/dt-1/download")
    assert response.status_code == 401


def test_dingtalk_download_returns_file(monkeypatch):
    monkeypatch.setattr(api, "get_dingtalk_file_provider", lambda: FakeFileProvider())
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: None)
    login_as(STAFF)
    response = client.get("/api/dingtalk/files/dt-1/download")
    logout()

    assert response.status_code == 200
    assert response.content == "文件内容".encode("utf-8")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_dingtalk_download_respects_visibility(monkeypatch):
    monkeypatch.setattr(api, "get_dingtalk_file_provider",
                        lambda: FakeFileProvider(visibility="staff"))
    login_as(NEWCOMER)
    response = client.get("/api/dingtalk/files/dt-1/download")
    logout()

    assert response.status_code == 403


def test_file_library_filters_by_department(monkeypatch):
    monkeypatch.setattr(api, "list_current_filenames", lambda: set())
    login_as(NEWCOMER)
    payload = client.get("/api/files").json()
    logout()

    names = {item["name"] for item in payload["files"]}
    assert "客服部 2026 中秋后战略安排.txt" in names
    assert "X20 Pro 充电说明.txt" in names
    assert "财务部 2026 中秋后战略安排.txt" not in names
    assert "薪资结构说明（内部）.txt" not in names


def test_file_library_hr_sees_sensitive(monkeypatch):
    monkeypatch.setattr(api, "list_current_filenames", lambda: set())
    login_as(HR)
    payload = client.get("/api/files").json()
    logout()

    salary = next(item for item in payload["files"]
                  if item["name"].startswith("薪资结构说明"))
    assert salary["sensitive"] is True
    assert salary["can_import"] is False


def test_file_import_uses_own_department(monkeypatch):
    class FakeKB:
        def upload(self, *args, **kwargs):
            return "[成功]测试导入"

    monkeypatch.setattr(api, "get_knowledge_base_service", lambda: FakeKB())
    monkeypatch.setattr(api, "require_model", lambda: None)
    monkeypatch.setattr(api, "log_audit", lambda *a, **kw: None)
    login_as(NEWCOMER)
    response = client.post("/api/files/dt-search-0001/import")
    logout()

    assert response.status_code == 200
    assert response.json()["department"] == "客户服务部"
    assert response.json()["visibility"] == "department"


def test_file_import_rejects_sensitive(monkeypatch):
    login_as(HR)
    response = client.post("/api/files/dt-search-0008/import")
    logout()

    assert response.status_code == 403
    assert "敏感" in response.json()["detail"]
