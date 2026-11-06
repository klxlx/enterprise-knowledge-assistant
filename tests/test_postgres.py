"""Integration checks run against a separate disposable PostgreSQL database."""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.embeddings import Embeddings

import config_data as config
import database
from database import ROLE_NEWCOMER, ROLE_STAFF
from knowledge_base import KnowledgeBaseService
from vector_stores import VectorStoreService

pytestmark = pytest.mark.skipif(
    not os.environ.get("RAG_RUN_DB_TESTS"), reason="requires an isolated test database"
)


class StubEmbeddings(Embeddings):
    def __init__(self):
        self.batch_sizes = []

    def embed_query(self, text):
        return ([1.0, 0.0] if "电池" in text else [0.0, 1.0]) + [0.0] * (config.embedding_dimensions - 2)

    def embed_documents(self, texts):
        self.batch_sizes.append(len(texts))
        return [self.embed_query(text) for text in texts]


@pytest.fixture(autouse=True)
def isolated_database():
    # Refuse destructive fixture cleanup unless pointed at the dedicated test DB.
    from sqlalchemy.engine import make_url
    assert make_url(config.database_url).database == "robot_rag_test"
    database.init_database()
    with database.connect() as connection:
        connection.execute("TRUNCATE rag_documents CASCADE")
        connection.execute("TRUNCATE employees")
        connection.execute("TRUNCATE handbook_entries")
        connection.execute("TRUNCATE qa_turns")
        connection.execute("TRUNCATE audit_logs")
    yield
    with database.connect() as connection:
        connection.execute("TRUNCATE rag_documents CASCADE")
        connection.execute("TRUNCATE employees")
        connection.execute("TRUNCATE handbook_entries")
        connection.execute("TRUNCATE qa_turns")
        connection.execute("TRUNCATE audit_logs")


def counts():
    with database.connect() as connection:
        return connection.execute(
            "SELECT (SELECT count(*) FROM rag_documents), (SELECT count(*) FROM rag_chunks)"
        ).fetchone()


def test_upload_retrieve_and_deduplicate(monkeypatch):
    # 用替代嵌入模型离线跑，避免真的调用重排序接口。
    monkeypatch.setattr(config, "rerank_enabled", False)
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload_by_str("电池保修一年。", "电池.txt")
    service.upload_by_str("拖布可拆卸清洗。", "拖布.txt")
    result = VectorStoreService(embedding).search("电池保修多久？")
    # 入库内容带“文档标题 · 章节”前缀，便于模型分辨片段出处。
    assert "电池保修一年。" in result[0].content
    assert result[0].source == "电池.txt"
    assert "跳过" in service.upload_by_str("电池保修一年。", "another.txt")
    assert counts() == (2, 2)


def test_embedding_failure_does_not_leave_dedup_record():
    class FailedEmbeddings(StubEmbeddings):
        def embed_documents(self, texts):
            raise RuntimeError("simulated embedding failure")
    with pytest.raises(RuntimeError):
        KnowledgeBaseService(FailedEmbeddings()).upload_by_str("电池资料", "test.txt")
    assert counts() == (0, 0)
    assert "成功" in KnowledgeBaseService(StubEmbeddings()).upload_by_str("电池资料", "test.txt")


def test_database_failure_rolls_back_document_and_chunks():
    # PostgreSQL text fields reject NUL; the document ledger must roll back too.
    with pytest.raises(Exception):
        database.insert_document("invalid", "test.txt", ["valid", "invalid\0text"],
                                 StubEmbeddings().embed_documents(["valid", "invalid"]), {})
    assert counts() == (0, 0)


def test_concurrent_upload_has_one_document():
    content = "电池资料"
    digest = hashlib.sha256(content.encode()).hexdigest()
    vector = StubEmbeddings().embed_query(content)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: database.insert_document(
            digest, "test.txt", [content], [vector], {}), range(2)))
    assert sorted(results) == [False, True]
    assert counts() == (1, 1)


def test_incompatible_embedding_dimensions_rejected():
    with pytest.raises(ValueError, match="维度"):
        database.insert_document("bad-dim", "test.txt", ["text"], [[1.0, 0.0]], {})
    assert counts() == (0, 0)


def test_model_change_rejected(monkeypatch):
    monkeypatch.setattr(config, "embedding_model_name", "different-model")
    with pytest.raises(ValueError, match="不匹配"):
        database.check_embedding_configuration()


def test_upload_batches_at_most_ten_chunks():
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload_by_str("电池资料。" * 3000, "long.txt")
    assert len(embedding.batch_sizes) > 1
    assert max(embedding.batch_sizes) <= 10


def test_reupload_same_filename_replaces_old_version(monkeypatch):
    """同名同范围重新上传 = 更新版本：旧版本必须下线，不能新旧一起被检索到。"""
    monkeypatch.setattr(config, "rerank_enabled", False)
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload("电池保修一年。", "电池说明.txt")
    service.upload("电池保修两年。", "电池说明.txt")

    with database.connect() as connection:
        current = connection.execute(
            "SELECT count(*) FROM rag_documents "
            "WHERE filename = '电池说明.txt' AND is_current"
        ).fetchone()[0]
        retired = connection.execute(
            "SELECT count(*) FROM rag_documents "
            "WHERE filename = '电池说明.txt' AND NOT is_current"
        ).fetchone()[0]
    assert (current, retired) == (1, 1)

    hits = VectorStoreService(embedding).search("电池保修多久？")
    joined = "\n".join(hit.content for hit in hits)
    assert "两年" in joined
    assert "一年" not in joined


def test_same_filename_different_scope_keeps_both_versions(monkeypatch):
    """同名但适用范围不同的资料是两份独立的文件，不能互相覆盖。"""
    monkeypatch.setattr(config, "rerank_enabled", False)
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload("电池保修一年。", "说明.txt", scope="X20 Pro")
    service.upload("电池保修两年。", "说明.txt", scope="T30")

    with database.connect() as connection:
        scopes = connection.execute(
            "SELECT scope FROM rag_documents WHERE filename = '说明.txt' AND is_current "
            "ORDER BY scope"
        ).fetchall()
    assert [row[0] for row in scopes] == ["T30", "X20 Pro"]

    x20_hits = VectorStoreService(embedding).search("电池保修多久？", scope="X20 Pro")
    assert any("一年" in hit.content for hit in x20_hits)
    assert all("两年" not in hit.content for hit in x20_hits)


def test_expired_document_drops_out_of_retrieval(monkeypatch):
    """设了失效日期的资料过期后必须退出检索，恢复日期后重新参与。"""
    monkeypatch.setattr(config, "rerank_enabled", False)
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload_by_str("电池保修一年。", "电池.txt")
    # 两份资料用同一个向量，命中与否只取决于时效过滤
    service.upload("2025 年度差旅标准：备用电池需随身携带。", "差旅标准.txt",
                   effective_from="2025-01-01", expires_at="2025-12-31")

    vector_service = VectorStoreService(embedding)
    # 差旅标准已经过期，即使向量完全命中也不该出现
    assert [hit.source for hit in vector_service.search("电池保修多久？")] == ["电池.txt"]

    database.set_validity("差旅标准.txt", expires_at=database.NO_EXPIRY_DATE)
    restored = vector_service.search("电池保修多久？")
    assert any(hit.source == "差旅标准.txt" for hit in restored)
    assert restored[0].expires_at == database.NO_EXPIRY_DATE


def test_department_visibility_isolates_newcomers(monkeypatch):
    """新人只能看到公开资料和自己部门；人事能跨部门；敏感资料不进问答。"""
    monkeypatch.setattr(config, "rerank_enabled", False)
    embedding = StubEmbeddings()
    service = KnowledgeBaseService(embedding)
    service.upload("电池客服资料。", "客服资料.txt", scope="客户服务部",
                   visibility="department", department="客户服务部")
    service.upload("电池财务资料。", "财务资料.txt", scope="财务部",
                   visibility="department", department="财务部")
    service.upload("电池公开资料。", "公开资料.txt", scope="通用",
                   visibility="all", department="通用")
    service.upload("电池主管资料。", "主管资料.txt", scope="客户服务部",
                   visibility="staff", department="客户服务部")
    service.upload("电池薪资资料。", "薪资资料.txt", scope="人力资源部",
                   visibility="sensitive", department="人力资源部")

    vector_service = VectorStoreService(embedding)

    newcomer = {hit.source for hit in vector_service.search(
        "电池保修多久？", role=ROLE_NEWCOMER, department="客户服务部")}
    assert {"客服资料.txt", "公开资料.txt"} <= newcomer
    assert "财务资料.txt" not in newcomer
    assert "主管资料.txt" not in newcomer
    assert "薪资资料.txt" not in newcomer

    service_supervisor = {hit.source for hit in vector_service.search(
        "电池保修多久？", role=ROLE_STAFF, department="客户服务部")}
    assert "主管资料.txt" in service_supervisor
    assert "财务资料.txt" not in service_supervisor

    hr = {hit.source for hit in vector_service.search(
        "电池保修多久？", role=ROLE_STAFF, department="人力资源部")}
    assert {"客服资料.txt", "财务资料.txt", "主管资料.txt"} <= hr
    # 敏感资料不会进问答，即使人事账号也不行
    assert "薪资资料.txt" not in hr


def test_chunk_validity_follows_document():
    """过滤跑在 rag_chunks 上：改文档时效时片段要同步，历史上不一致的数据要自愈。"""
    embedding = StubEmbeddings()
    KnowledgeBaseService(embedding).upload(
        "电池保修一年。", "电池.txt", expires_at="2026-12-31")

    def expires_dates():
        with database.connect() as connection:
            return connection.execute(
                "SELECT (SELECT expires_at FROM rag_documents WHERE filename = '电池.txt'),"
                "       (SELECT expires_at FROM rag_chunks WHERE document_hash = "
                "        (SELECT document_hash FROM rag_documents WHERE filename = '电池.txt'))"
            ).fetchone()

    assert [value.isoformat() for value in expires_dates()] == ["2026-12-31", "2026-12-31"]

    # 模拟历史数据：文档有时效，片段还停在默认值
    with database.connect() as connection:
        connection.execute(
            "UPDATE rag_chunks SET expires_at = '9999-12-31' "
            "WHERE document_hash = (SELECT document_hash FROM rag_documents "
            "WHERE filename = '电池.txt')"
        )
    database.migrate_database()
    assert [value.isoformat() for value in expires_dates()] == ["2026-12-31", "2026-12-31"]

    # 批量改日期时两边也要一起动
    database.set_validity("电池.txt", expires_at="2027-06-30")
    assert [value.isoformat() for value in expires_dates()] == ["2027-06-30", "2027-06-30"]


def test_employee_cards_respect_visibility():
    database.upsert_employee({
        "employee_id": "emp-open", "name": "公开员工", "department": "行政部",
        "responsibilities": "办公用品", "contact": "分机 1", "visibility": "all",
    })
    database.upsert_employee({
        "employee_id": "emp-internal", "name": "内部员工", "department": "人力资源部",
        "responsibilities": "薪资核算", "contact": "分机 2", "visibility": "staff",
    })

    newcomer_names = [item["name"] for item in database.list_employees(role=ROLE_NEWCOMER)]
    staff_names = [item["name"] for item in database.list_employees(role=ROLE_STAFF)]

    assert newcomer_names == ["公开员工"]
    assert set(staff_names) == {"公开员工", "内部员工"}


def test_delete_employee_reports_referenced_processes():
    database.upsert_employee({
        "employee_id": "emp-owner", "name": "刘洋", "department": "财务部",
        "responsibilities": "报销审核", "contact": "分机 8005", "visibility": "all",
    })
    database.upsert_handbook_entry({
        "entry_id": "process-reimburse", "kind": "process", "title": "报销流程",
        "steps": ["填报销单", "主管签字"], "keywords": "报销",
        "owner": "emp-owner", "visibility": "all",
    })

    removed = database.delete_employee("emp-owner")

    assert removed["name"] == "刘洋"
    assert removed["referenced_by"] == ["报销流程"]
    assert database.list_employees(role=ROLE_STAFF) == []
    assert database.delete_employee("emp-owner") is None


def test_handbook_entries_filter_by_kind_and_role():
    database.upsert_handbook_entry({
        "entry_id": "faq-open", "kind": "faq", "title": "入职要带什么",
        "question": "入职第一天要带什么？", "answer": "身份证和照片。",
        "keywords": "入职|材料", "visibility": "all",
    })
    database.upsert_handbook_entry({
        "entry_id": "faq-staff", "kind": "faq", "title": "薪资什么时候发",
        "question": "工资什么时候发？", "answer": "每月 15 日。",
        "keywords": "薪资", "visibility": "staff",
    })
    database.upsert_handbook_entry({
        "entry_id": "process-001", "kind": "process", "title": "报销流程",
        "steps": ["填报销单", "主管签字"], "links": ["https://oa.example.com"],
        "keywords": "报销", "visibility": "all",
    })

    newcomer_entries = database.list_handbook_entries(role=ROLE_NEWCOMER)
    staff_faq = database.list_handbook_entries(kind="faq", role=ROLE_STAFF)
    processes = database.list_handbook_entries(kind="process", role=ROLE_NEWCOMER)

    assert [item["entry_id"] for item in newcomer_entries] == ["faq-open", "process-001"]
    assert {item["entry_id"] for item in staff_faq} == {"faq-open", "faq-staff"}
    assert processes[0]["steps"] == ["填报销单", "主管签字"]


def test_blind_spots_collect_unsupported_negative_and_handoff():
    """盲区清单的三种口径：没依据、被点没用、转人工。"""
    database.record_qa_turn("user_1", "公司年会在哪办？", "没有查到相关信息。", "",
                            username="newbie", role=ROLE_NEWCOMER)
    good = database.record_qa_turn("user_1", "报销怎么走？", "在 OA 提交报销单。",
                                   "process", tool_count=1, username="newbie",
                                   role=ROLE_NEWCOMER)
    handoff = database.record_qa_turn("user_1", "机器有异响怎么办？", "已转人工。", "",
                                      handoff=True, username="newbie",
                                      role=ROLE_NEWCOMER)
    database.record_qa_turn("user_2", "报销怎么走？", "在 OA 提交报销单。",
                            "process", tool_count=1, username="staff", role=ROLE_STAFF)

    assert database.set_feedback(good, -1, "步骤不全") is True
    assert database.set_feedback(good, 1, "看错了") is True   # 允许改评价

    spots = database.list_blind_spots(days=30)
    questions = {spot["question"] for spot in spots}

    assert questions == {"公司年会在哪办？", "机器有异响怎么办？"}
    assert all(spot["times"] == 1 for spot in spots)
    # 改成正评之后就不再算盲区
    assert database.get_qa_turn(good)["feedback"] == 1
    assert database.get_qa_turn(handoff)["question"] == "机器有异响怎么办？"


def test_feedback_stats_counts_ratings_and_gaps():
    positive = database.record_qa_turn("user_1", "问题A", "答案A", "knowledge")
    negative = database.record_qa_turn("user_1", "问题B", "答案B", "")
    database.record_qa_turn("user_1", "问题C", "答案C", "faq")
    database.set_feedback(positive, 1)
    database.set_feedback(negative, -1, "没答到点上")

    stats = database.feedback_stats(days=30)

    assert stats["turns"] == 3
    assert (stats["positive"], stats["negative"], stats["rated"]) == (1, 1, 2)
    assert stats["satisfaction"] == 0.5
    assert stats["unsupported"] == 1      # 只有「问题B」是没有任何依据答的

    entries = database.list_feedback_entries(days=30, rating=-1)
    assert [entry["question"] for entry in entries] == ["问题B"]
    assert entries[0]["comment"] == "没答到点上"


def test_feedback_rejects_unknown_rating():
    turn_id = database.record_qa_turn("user_1", "问题", "答案", "knowledge")
    with pytest.raises(ValueError):
        database.set_feedback(turn_id, 0)
    assert database.set_feedback(999999, 1) is False


def test_delete_qa_turns_only_removes_own_records():
    database.record_qa_turn("user_1", "问题A", "回答A", "", username="alice")
    database.record_qa_turn("user_1", "问题B", "回答B", "", username="bob")
    database.record_qa_turn("user_2", "问题C", "回答C", "", username="alice")

    assert database.delete_qa_turns("user_1", "alice") == 1
    remaining = database.list_session_turns("user_1")
    assert [turn["question"] for turn in remaining] == ["问题B"]
    # 别的会话不受影响
    assert len(database.list_session_turns("user_2")) == 1


def test_qa_turn_keeps_file_attachments():
    """钉盘文件卡片要跟着问答一起存下来，重开会话还能下载。"""
    turn_id = database.record_qa_turn(
        "user_1", "找中秋战略文件", "找到 2 份", "dingtalk",
        files=[{"file_id": "dt-1", "name": "客服部战略.txt"},
               {"file_id": "dt-2", "name": "财务部战略.txt"}],
    )

    turns = database.list_session_turns("user_1")

    assert turns[0]["id"] == turn_id
    assert [item["name"] for item in turns[0]["files"]] == [
        "客服部战略.txt", "财务部战略.txt"]


def test_turns_with_files_are_not_blind_spots():
    """有钉盘文件依据的问答不能算盲区，即使历史数据里 evidence 还没回填。"""
    database.record_qa_turn(
        "user_1", "找钉盘里的战略文件", "找到 1 份", "",
        files=[{"file_id": "dt-1", "name": "客服部战略.txt"}],
    )
    database.record_qa_turn("user_2", "公司年会在哪办？", "没有找到相关信息", "")

    spots = database.list_blind_spots(days=30)

    assert [spot["question"] for spot in spots] == ["公司年会在哪办？"]
    # 无依据统计也只算真正什么都没有的那轮
    assert database.feedback_stats(days=30)["unsupported"] == 1


def test_migration_backfills_dingtalk_evidence():
    turn_id = database.record_qa_turn(
        "user_1", "张伟发的资料", "找到 1 份", "",
        files=[{"file_id": "dt-1", "name": "薪资结构说明.txt"}],
    )
    database.migrate_database()

    turn = database.list_session_turns("user_1")[0]
    assert turn["id"] == turn_id
    assert turn["evidence"] == "dingtalk"


def test_permission_denied_is_not_a_blind_spot():
    """资料存在但当前账号无权查看，属于权限问题，不该让员工去补知识库。"""
    turn_id = database.record_qa_turn(
        "user_1", "张伟发的资料", "这些资料仅员工可见，当前账号没有查看权限。",
        "permission_denied", username="newbie", role=ROLE_NEWCOMER,
    )
    database.set_feedback(turn_id, -1, "我看不到")

    # 盲区清单只放知识缺口；权限拒绝即使被点差评也不算
    assert database.list_blind_spots(days=30) == []
    assert database.feedback_stats(days=30)["unsupported"] == 0
    assert database.feedback_stats(days=30)["negative"] == 1
    # 差评明细仍然能看到这条，方便管理员核实权限配置
    assert database.list_feedback_entries(days=30)[0]["id"] == turn_id


def test_audit_log_round_trip():
    database.log_audit("knowledge.upload", username="staff", role=ROLE_STAFF,
                       target="制度.txt", detail={"scope": "人力资源部"})
    database.log_audit("auth.login", username="staff", role=ROLE_STAFF, status="failed")

    logs = database.list_audit_logs(limit=10)
    assert [entry["action"] for entry in logs] == ["auth.login", "knowledge.upload"]
    assert logs[1]["detail"] == {"scope": "人力资源部"}

    filtered = database.list_audit_logs(action="knowledge.upload")
    assert len(filtered) == 1 and filtered[0]["target"] == "制度.txt"
    by_user = database.list_audit_logs(username="staff")
    assert len(by_user) == 2
    assert database.list_audit_logs(username="nobody") == []
