"""Database schema, model compatibility checks and transactional knowledge writes."""

import logging
import math
import re
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from pgvector.psycopg import Vector, register_vector
from psycopg.types.json import Jsonb

import config_data as config

logger = logging.getLogger(__name__)

# 通用资料的型号标记：检索某个型号时，通用资料始终参与
GENERIC_MODEL = "通用"
# 没有明确失效期的资料用这个远期日期，避免 NULL 参与比较时被过滤掉
NO_EXPIRY_DATE = "9999-12-31"

# 资料可见性：all=全员，department=本部门，staff=仅主管，sensitive=仅人事
DOCUMENT_VISIBILITIES = ("all", "department", "staff", "sensitive")


def normalize_document_visibility(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in DOCUMENT_VISIBILITIES else "all"


def normalize_department(value: str | None) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or GENERIC_MODEL


# 一份资料可能同时覆盖多个型号（例如同一本手册同时适用于 X5 MAX 和 X5 PRO）
_MODEL_SEPARATORS = re.compile(r"[,，、;/]+")


def normalize_scope(value: str | None) -> str:
    """「适用范围」归一化：去首尾空白、压缩中间空白、去重，多个取值用「, 」连接。

    适用范围可以填产品型号、部门或业务线。不改大小写，方便界面上按用户输入的写法展示；
    匹配时用大小写不敏感、包含式的过滤条件，这样「DEEBOT X5」也能命中标着
    「DEEBOT X5 MAX, DEEBOT X5 PRO」的资料。
    """
    if value is None:
        return GENERIC_MODEL
    models = []
    for piece in _MODEL_SEPARATORS.split(str(value)):
        cleaned = " ".join(piece.split())
        if cleaned and cleaned not in models:
            models.append(cleaned)
    return ", ".join(models) or GENERIC_MODEL


# 角色：员工权限高于新人
ROLE_STAFF = "staff"
ROLE_NEWCOMER = "newcomer"
ROLES = (ROLE_STAFF, ROLE_NEWCOMER)

# 可见性：all=全员可见，staff=仅员工可见
VISIBILITY_ALL = "all"
VISIBILITY_STAFF = "staff"
VISIBILITIES = (VISIBILITY_ALL, VISIBILITY_STAFF)


def normalize_visibility(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in VISIBILITIES else VISIBILITY_ALL


def visible_levels(role: str) -> list[str]:
    """某个角色能看到哪些可见性等级：新人只能看全员资料，员工还能看内部资料。"""
    if role == ROLE_STAFF:
        return [VISIBILITY_ALL, VISIBILITY_STAFF]
    return [VISIBILITY_ALL]

# 给已有库补列用；全部是幂等语句，可以重复执行
MIGRATION_STATEMENTS = (
    # 把「产品型号」泛化成「适用范围」：型号、部门、业务线都能放进去
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'rag_documents' AND column_name = 'product_model')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'rag_documents' AND column_name = 'scope') THEN
            ALTER TABLE rag_documents RENAME COLUMN product_model TO scope;
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'rag_chunks' AND column_name = 'product_model')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'rag_chunks' AND column_name = 'scope') THEN
            ALTER TABLE rag_chunks RENAME COLUMN product_model TO scope;
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'handoff_tickets' AND column_name = 'product_model')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'handoff_tickets' AND column_name = 'scope') THEN
            ALTER TABLE handoff_tickets RENAME COLUMN product_model TO scope;
        END IF;
    END $$;
    """,
    # 文档表：型号、来源、版本
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT '通用'",
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS source_type text NOT NULL DEFAULT 'upload'",
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS source_key text",
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true",
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()",
    # 片段表：过滤条件必须落在真实列上，所以型号和版本都要冗余一份
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT '通用'",
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true",
    # 可见性：all=全员可见，staff=仅员工可见
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'all'",
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'all'",
    # 部门归属：department=本部门可见，sensitive=仅人事；通用资料不限制部门
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS department text NOT NULL DEFAULT '通用'",
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS department text NOT NULL DEFAULT '通用'",
    # 老资料如果是按部门命名的，把 scope 回填成部门
    "UPDATE rag_documents SET department = scope WHERE department = '通用' "
    "AND scope IN ('客户服务部','人力资源部','行政部','财务部','供应链部',"
    "'研发部','市场部','信息技术部')",
    "UPDATE rag_chunks c SET department = d.department FROM rag_documents d "
    "WHERE c.document_hash = d.document_hash AND c.department IS DISTINCT FROM d.department",
    # 从飞书同步进来的部门资料，改为「本部门可见」（通用资料保持公开）
    "UPDATE rag_documents SET visibility = 'department' "
    "WHERE source_type = 'feishu' AND department <> '通用' AND visibility = 'all'",
    "UPDATE rag_chunks c SET visibility = d.visibility FROM rag_documents d "
    "WHERE c.document_hash = d.document_hash AND c.visibility IS DISTINCT FROM d.visibility",
    # 时效：生效日期和失效日期。没有失效期的用远期日期，避免 NULL 参与比较时被过滤掉
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS effective_from date NOT NULL DEFAULT CURRENT_DATE",
    "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS expires_at date NOT NULL DEFAULT '9999-12-31'",
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS effective_from date NOT NULL DEFAULT CURRENT_DATE",
    "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS expires_at date NOT NULL DEFAULT '9999-12-31'",
    # 老数据没有来源标识，用内容哈希补一个
    "UPDATE rag_documents SET source_key = 'upload:' || document_hash WHERE source_key IS NULL",
    # 老资料刚加日期字段时默认成了今天，这里回填成它们的实际入库日期
    "UPDATE rag_documents SET effective_from = created_at::date "
    "WHERE effective_from = CURRENT_DATE AND created_at::date < CURRENT_DATE",
    # 片段的时效必须跟着文档走：过滤是在 rag_chunks 上做的，两边不一致会漏筛或误筛
    "UPDATE rag_chunks c SET effective_from = d.effective_from, expires_at = d.expires_at "
    "FROM rag_documents d "
    "WHERE c.document_hash = d.document_hash "
    "AND (c.effective_from IS DISTINCT FROM d.effective_from "
    "     OR c.expires_at IS DISTINCT FROM d.expires_at)",
    # 检索过滤用的索引
    "DROP INDEX IF EXISTS rag_chunks_model_current",
    "CREATE INDEX IF NOT EXISTS rag_chunks_scope_visibility ON rag_chunks (scope, visibility, is_current)",
    # 同一个来源只允许一个当前版本，旧版本可以共存
    "CREATE UNIQUE INDEX IF NOT EXISTS rag_documents_source_key_current "
    "ON rag_documents (source_key) WHERE is_current",
    # 账号与登录令牌
    """
    CREATE TABLE IF NOT EXISTS users (
        username text PRIMARY KEY,
        display_name text NOT NULL,
        role text NOT NULL CHECK (role IN ('staff', 'newcomer')),
        department text NOT NULL DEFAULT '',
        password_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_tokens (
        token text PRIMARY KEY,
        username text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS auth_tokens_username ON auth_tokens (username)",
    # 员工卡片：新人提问"这事找谁"时返回，也是通讯录的数据源
    """
    CREATE TABLE IF NOT EXISTS employees (
        employee_id text PRIMARY KEY,
        name text NOT NULL,
        department text NOT NULL,
        title text NOT NULL DEFAULT '',
        employee_type text NOT NULL DEFAULT '员工',
        responsibilities text NOT NULL DEFAULT '',
        contact text NOT NULL DEFAULT '',
        keywords text NOT NULL DEFAULT '',
        scope text NOT NULL DEFAULT '通用',
        visibility text NOT NULL DEFAULT 'all',
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE employees ADD COLUMN IF NOT EXISTS employee_type "
    "text NOT NULL DEFAULT '员工'",
    "CREATE INDEX IF NOT EXISTS employees_department ON employees (department)",
    # 结构化知识：FAQ（问答）和流程（步骤+链接）
    """
    CREATE TABLE IF NOT EXISTS handbook_entries (
        entry_id text PRIMARY KEY,
        kind text NOT NULL CHECK (kind IN ('faq', 'process')),
        title text NOT NULL,
        question text NOT NULL DEFAULT '',
        answer text NOT NULL DEFAULT '',
        steps jsonb NOT NULL DEFAULT '[]'::jsonb,
        links jsonb NOT NULL DEFAULT '[]'::jsonb,
        keywords text NOT NULL DEFAULT '',
        scope text NOT NULL DEFAULT '通用',
        visibility text NOT NULL DEFAULT 'all',
        owner text NOT NULL DEFAULT '',
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS handbook_entries_kind ON handbook_entries (kind)",
    # 转人工工单
    """
    CREATE TABLE IF NOT EXISTS handoff_tickets (
        ticket_no text PRIMARY KEY,
        session_id text NOT NULL,
        scope text NOT NULL DEFAULT '通用',
        reason text NOT NULL,
        status text NOT NULL DEFAULT 'open',
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS handoff_tickets_session "
    "ON handoff_tickets (session_id, created_at DESC)",
    # 每轮问答的留痕：反馈、知识盲区清单、答不上来率都从这张表算
    """
    CREATE TABLE IF NOT EXISTS qa_turns (
        id bigserial PRIMARY KEY,
        session_id text NOT NULL,
        username text NOT NULL DEFAULT '',
        role text NOT NULL DEFAULT 'newcomer',
        question text NOT NULL,
        answer text NOT NULL DEFAULT '',
        evidence text NOT NULL DEFAULT '',
        source_count integer NOT NULL DEFAULT 0,
        tool_count integer NOT NULL DEFAULT 0,
        handoff boolean NOT NULL DEFAULT false,
        files jsonb NOT NULL DEFAULT '[]'::jsonb,
        feedback smallint,
        feedback_comment text NOT NULL DEFAULT '',
        feedback_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE qa_turns ADD COLUMN IF NOT EXISTS files jsonb NOT NULL DEFAULT '[]'::jsonb",
    # 早期钉盘检索没有写 evidence，回填一下，避免这些有文件依据的问答被算成盲区
    "UPDATE qa_turns SET evidence = 'dingtalk' "
    "WHERE evidence = '' AND files <> '[]'::jsonb",
    "CREATE INDEX IF NOT EXISTS qa_turns_created ON qa_turns (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS qa_turns_feedback ON qa_turns (feedback) "
    "WHERE feedback IS NOT NULL",
    # 审计日志：谁在什么时候动了什么，知识库这类共享资源必须留痕
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        username text NOT NULL DEFAULT '',
        role text NOT NULL DEFAULT '',
        action text NOT NULL,
        target text NOT NULL DEFAULT '',
        detail jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'ok'
    )
    """,
    "CREATE INDEX IF NOT EXISTS audit_logs_created ON audit_logs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_logs_action "
    "ON audit_logs (action, created_at DESC)",
)


def connect():
    if not config.database_url:
        raise ValueError("DATABASE_URL must be configured")
    conninfo = config.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(conninfo, connect_timeout=5, options="-c statement_timeout=15000")


def check_embedding_configuration(connection=None):
    if connection is None:
        with connect() as connection:
            return check_embedding_configuration(connection)
    row = connection.execute(
        "SELECT embedding_model, dimensions FROM rag_configuration WHERE id = 1"
    ).fetchone()
    if row != (config.embedding_model_name, config.embedding_dimensions):
        raise ValueError("数据库的嵌入模型或维度与配置不匹配，请先迁移知识库")


def init_database():
    """Run explicitly at deployment; never overwrite an existing embedding space."""
    with connect() as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone():
            raise ValueError("请数据库管理员先启用 pgvector 扩展")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS rag_configuration (
                id integer PRIMARY KEY CHECK (id = 1),
                embedding_model text NOT NULL, dimensions integer NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO rag_configuration VALUES (1, %s, %s) ON CONFLICT DO NOTHING",
            (config.embedding_model_name, config.embedding_dimensions),
        )
        check_embedding_configuration(connection)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS rag_documents (
                document_hash text PRIMARY KEY, filename text NOT NULL,
                chunk_count integer NOT NULL CHECK (chunk_count > 0),
                created_at timestamptz NOT NULL DEFAULT now(),
                scope text NOT NULL DEFAULT '通用',
                visibility text NOT NULL DEFAULT 'all',
                source_type text NOT NULL DEFAULT 'upload',
                source_key text,
                is_current boolean NOT NULL DEFAULT true,
                updated_at timestamptz NOT NULL DEFAULT now(),
                effective_from date NOT NULL DEFAULT CURRENT_DATE,
                expires_at date NOT NULL DEFAULT '9999-12-31'
            )
        """)
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id uuid PRIMARY KEY,
                document_hash text NOT NULL REFERENCES rag_documents(document_hash) ON DELETE CASCADE,
                content text NOT NULL,
                embedding vector({config.embedding_dimensions}) NOT NULL,
                metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                scope text NOT NULL DEFAULT '通用',
                visibility text NOT NULL DEFAULT 'all',
                is_current boolean NOT NULL DEFAULT true,
                effective_from date NOT NULL DEFAULT CURRENT_DATE,
                expires_at date NOT NULL DEFAULT '9999-12-31'
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw
            ON rag_chunks USING hnsw (embedding vector_cosine_ops)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS rag_chunks_document_hash ON rag_chunks (document_hash)
        """)
    migrate_database()


def migrate_database():
    """幂等迁移：给已经建好的库补上后加的列、索引和表。"""
    with connect() as connection:
        for statement in MIGRATION_STATEMENTS:
            connection.execute(statement)


def database_ready() -> bool:
    try:
        with connect() as connection:
            check_embedding_configuration(connection)
            connection.execute("SELECT id FROM rag_chunks LIMIT 1")
        return True
    except (psycopg.Error, ValueError):
        return False


def document_exists(document_hash: str) -> bool:
    with connect() as connection:
        check_embedding_configuration(connection)
        return connection.execute(
            "SELECT 1 FROM rag_documents WHERE document_hash = %s", (document_hash,)
        ).fetchone() is not None


def list_scopes() -> list[str]:
    """当前生效资料里出现过的适用范围，供前端做下拉建议。"""
    with connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT scope FROM rag_documents WHERE is_current ORDER BY scope"
        ).fetchall()
    return [row[0] for row in rows]


def list_current_filenames() -> set[str]:
    """当前知识库里的文件名集合，用于标记「这份文件已导入」。"""
    with connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT filename FROM rag_documents WHERE is_current"
        ).fetchall()
    return {row[0] for row in rows}


def create_handoff_ticket(session_id: str, reason: str, scope: str | None = None,
                          dedup_minutes: int = 10) -> tuple[str, bool]:
    """转人工建单，返回（工单号, 是否新建）。

    同一个会话在 dedup_minutes 分钟内已有未关闭工单时复用，避免用户连点建出一堆重复工单。
    """
    model = normalize_scope(scope)
    with connect() as connection:
        existing = connection.execute(
            "SELECT ticket_no FROM handoff_tickets "
            "WHERE session_id = %s AND status = 'open' "
            "AND created_at > now() - make_interval(mins => %s) "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, dedup_minutes),
        ).fetchone()
        if existing:
            return existing[0], False

        ticket_no = f"HD-{datetime.now():%Y%m%d}-{uuid4().hex[:6].upper()}"
        connection.execute(
            "INSERT INTO handoff_tickets (ticket_no, session_id, scope, reason) "
            "VALUES (%s, %s, %s, %s)",
            (ticket_no, session_id, model, reason),
        )
    return ticket_no, True


def list_handoff_tickets(limit: int = 50) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT ticket_no, session_id, scope, reason, status, created_at "
            "FROM handoff_tickets ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "ticket_no": row[0], "session_id": row[1], "scope": row[2],
            "reason": row[3], "status": row[4], "created_at": row[5].isoformat(),
        }
        for row in rows
    ]


def list_source_documents(source_type: str) -> list[dict]:
    """某个数据源当前生效的文档，供增量同步比对。"""
    with connect() as connection:
        rows = connection.execute(
            "SELECT document_hash, filename, source_key, scope "
            "FROM rag_documents WHERE source_type = %s AND is_current",
            (source_type,),
        ).fetchall()
    return [
        {"document_hash": row[0], "filename": row[1],
         "source_key": row[2], "scope": row[3]}
        for row in rows
    ]


def retire_document(document_hash: str) -> None:
    """把一份文档标记为历史版本，它和它的片段都不再参与检索。"""
    with connect() as connection:
        connection.execute(
            "UPDATE rag_documents SET is_current = false, updated_at = now() "
            "WHERE document_hash = %s", (document_hash,),
        )
        connection.execute(
            "UPDATE rag_chunks SET is_current = false WHERE document_hash = %s",
            (document_hash,),
        )


def set_scope(filename: str, new_scope: str) -> tuple[int, int]:
    """按文件名改「适用范围」，返回（改动的文档数, 改动的片段数）。

    适用范围只用于过滤、不参与向量化，所以改标签不需要重新嵌入。
    两张表都要改：文档表决定列表和下拉显示，片段表才是检索过滤真正读的。
    """
    model = normalize_scope(new_scope)
    with connect() as connection:
        documents = connection.execute(
            "UPDATE rag_documents SET scope = %s, updated_at = now() "
            "WHERE filename = %s AND is_current",
            (model, filename),
        ).rowcount
        chunks = connection.execute(
            "UPDATE rag_chunks SET scope = %s, "
            "metadata = metadata || jsonb_build_object('scope', %s::text) "
            "WHERE document_hash IN ("
            "  SELECT document_hash FROM rag_documents WHERE filename = %s AND is_current)",
            (model, model, filename),
        ).rowcount
    return documents or 0, chunks or 0


def set_visibility(filename: str, visibility: str) -> tuple[int, int]:
    """改资料的可见性（all=新人也能看，staff=仅员工可见）。"""
    level = normalize_document_visibility(visibility)
    with connect() as connection:
        documents = connection.execute(
            "UPDATE rag_documents SET visibility = %s, updated_at = now() "
            "WHERE filename = %s AND is_current",
            (level, filename),
        ).rowcount
        chunks = connection.execute(
            "UPDATE rag_chunks SET visibility = %s "
            "WHERE document_hash IN ("
            "  SELECT document_hash FROM rag_documents WHERE filename = %s AND is_current)",
            (level, filename),
        ).rowcount
    return documents or 0, chunks or 0


def set_validity(filename: str, effective_from: str | None = None,
                 expires_at: str | None = None) -> tuple[int, int]:
    """改资料的生效/失效日期（空值表示不改）。"""
    updates, params = [], []
    if effective_from:
        updates.append("effective_from = %s")
        params.append(effective_from)
    if expires_at:
        updates.append("expires_at = %s")
        params.append(expires_at)
    if not updates:
        return 0, 0

    with connect() as connection:
        documents = connection.execute(
            f"UPDATE rag_documents SET {', '.join(updates)}, updated_at = now() "
            "WHERE filename = %s AND is_current",
            (*params, filename),
        ).rowcount
        chunks = connection.execute(
            f"UPDATE rag_chunks SET {', '.join(updates)} "
            "WHERE document_hash IN ("
            "  SELECT document_hash FROM rag_documents WHERE filename = %s AND is_current)",
            (*params, filename),
        ).rowcount
    return documents or 0, chunks or 0


def insert_document(document_hash, filename, chunks, vectors, metadata,
                    scope=None, visibility="all", source_type="upload",
                    source_key=None, effective_from=None,
                    expires_at=None, replace_existing: bool = False,
                    department=None) -> bool:
    """写入一份文档。

    replace_existing=True 时，同名 + 同适用范围的老版本会在同一个事务里下线：
    界面重新上传修订版时旧内容必须立刻退出检索，否则新旧两份会一起被召回。
    按来源同步（source_key 非空）走自己的版本逻辑，不用这个开关。
    """
    if not chunks or len(chunks) != len(vectors):
        raise ValueError("文本片段与向量数量不一致")
    for vector in vectors:
        if len(vector) != config.embedding_dimensions or not all(math.isfinite(v) for v in vector):
            raise ValueError("嵌入向量维度不正确或包含非有限值")
        if not any(vector):
            raise ValueError("嵌入向量不能全为零")
    with connect() as connection:
        register_vector(connection)
        check_embedding_configuration(connection)
        model = normalize_scope(scope)
        dept = normalize_department(department)
        level = normalize_document_visibility(visibility)
        effective = effective_from or date.today().isoformat()
        expires = expires_at or NO_EXPIRY_DATE
        inserted = connection.execute(
            "INSERT INTO rag_documents "
            "(document_hash, filename, chunk_count, scope, visibility, source_type, "
            "source_key, effective_from, expires_at, department) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING document_hash",
            (document_hash, filename, len(chunks), model, level,
             source_type, source_key or f"{source_type}:{document_hash}",
             effective, expires, dept),
        ).fetchone()
        if inserted is None:
            return False
        if replace_existing:
            connection.execute(
                "UPDATE rag_chunks SET is_current = false "
                "WHERE is_current AND document_hash IN ("
                "  SELECT document_hash FROM rag_documents "
                "  WHERE filename = %s AND scope = %s AND department = %s AND is_current "
                "  AND document_hash <> %s)",
                (filename, model, dept, document_hash),
            )
            connection.execute(
                "UPDATE rag_documents SET is_current = false, updated_at = now() "
                "WHERE filename = %s AND scope = %s AND department = %s AND is_current "
                "AND document_hash <> %s",
                (filename, model, dept, document_hash),
            )
        rows = [
            (uuid5(NAMESPACE_URL, f"{document_hash}:{index}"), document_hash, text,
             Vector(vector), Jsonb({**metadata, "chunk_index": index, "scope": model,
                                    "department": dept}),
             model, level, effective, expires, dept)
            for index, (text, vector) in enumerate(zip(chunks, vectors))
        ]
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO rag_chunks "
                "(id, document_hash, content, embedding, metadata, scope, visibility, "
                "effective_from, expires_at, department) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows,
            )
    return True


def upsert_employee(employee: dict) -> None:
    """写入或更新一张员工卡片。"""
    with connect() as connection:
        connection.execute(
            "INSERT INTO employees (employee_id, name, department, title, employee_type, "
            "responsibilities, contact, keywords, scope, visibility) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (employee_id) DO UPDATE SET name = EXCLUDED.name, "
            "department = EXCLUDED.department, title = EXCLUDED.title, "
            "employee_type = EXCLUDED.employee_type, "
            "responsibilities = EXCLUDED.responsibilities, contact = EXCLUDED.contact, "
            "keywords = EXCLUDED.keywords, scope = EXCLUDED.scope, "
            "visibility = EXCLUDED.visibility, updated_at = now()",
            (employee["employee_id"], employee["name"], employee["department"],
             employee.get("title", ""), employee.get("employee_type", "员工"),
             employee.get("responsibilities", ""),
             employee.get("contact", ""), employee.get("keywords", ""),
             normalize_scope(employee.get("scope")),
             normalize_visibility(employee.get("visibility"))),
        )


def delete_employee(employee_id: str) -> dict | None:
    """删除一张员工卡片；返回被删的人，以及还把他当负责人的流程。

    不阻止删除（人员离职是正常情况），但把引用关系返回给调用方，
    让页面/日志里能看到"这个人的流程需要重新指派"。
    """
    with connect() as connection:
        row = connection.execute(
            "SELECT name, department FROM employees WHERE employee_id = %s",
            (employee_id,),
        ).fetchone()
        if row is None:
            return None
        referenced = connection.execute(
            "SELECT title FROM handbook_entries WHERE owner = %s ORDER BY title",
            (employee_id,),
        ).fetchall()
        connection.execute("DELETE FROM employees WHERE employee_id = %s", (employee_id,))
    return {"name": row[0], "department": row[1],
            "referenced_by": [item[0] for item in referenced]}


def list_employees(role: str = ROLE_NEWCOMER, department: str | None = None) -> list[dict]:
    """按角色列出可见的员工卡片。"""
    query = (
        "SELECT employee_id, name, department, title, employee_type, responsibilities, "
        "contact, keywords, scope, visibility FROM employees WHERE visibility = ANY(%s)"
    )
    params: list = [visible_levels(role)]
    if department:
        query += " AND department = %s"
        params.append(department)
    query += " ORDER BY department, employee_id"
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {"employee_id": row[0], "name": row[1], "department": row[2], "title": row[3],
         "employee_type": row[4], "responsibilities": row[5], "contact": row[6],
         "keywords": row[7], "scope": row[8], "visibility": row[9]}
        for row in rows
    ]


def match_employees(topic: str, employees: list[dict], limit: int = 3) -> list[dict]:
    """按事项关键词匹配员工卡片：关键词命中越多越靠前，命中部门名额外加权。

    纯函数，方便单测。关键词是运营这边维护的标签（逗号/竖线分隔都行）。
    """
    text = "".join(str(topic).split()).lower()
    if not text:
        return []

    scored = []
    for employee in employees:
        keywords = [
            piece.strip()
            for piece in re.split(r"[|,，、/]", employee.get("keywords") or "")
            if piece.strip()
        ]
        score = sum(1 for keyword in keywords if keyword.lower() in text)
        if employee.get("department") and employee["department"].lower() in text:
            score += 2
        if employee.get("name") and employee["name"].lower() in text:
            score += 3
        if score:
            scored.append((score, employee))

    scored.sort(key=lambda pair: (-pair[0], pair[1].get("employee_id", "")))
    return [employee for _, employee in scored[:limit]]


def search_employees(topic: str, role: str = ROLE_NEWCOMER, limit: int = 3) -> list[dict]:
    """在角色可见的范围内，按事项找负责的同事。"""
    return match_employees(topic, list_employees(role=role), limit=limit)


def upsert_handbook_entry(entry: dict) -> None:
    """写入或更新一条结构化知识（FAQ 或流程）。"""
    with connect() as connection:
        connection.execute(
            "INSERT INTO handbook_entries (entry_id, kind, title, question, answer, "
            "steps, links, keywords, scope, visibility, owner) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (entry_id) DO UPDATE SET kind = EXCLUDED.kind, "
            "title = EXCLUDED.title, question = EXCLUDED.question, answer = EXCLUDED.answer, "
            "steps = EXCLUDED.steps, links = EXCLUDED.links, keywords = EXCLUDED.keywords, "
            "scope = EXCLUDED.scope, visibility = EXCLUDED.visibility, "
            "owner = EXCLUDED.owner, updated_at = now()",
            (entry["entry_id"], entry["kind"], entry["title"],
             entry.get("question", ""), entry.get("answer", ""),
             Jsonb(entry.get("steps") or []), Jsonb(entry.get("links") or []),
             entry.get("keywords", ""), normalize_scope(entry.get("scope")),
             normalize_visibility(entry.get("visibility")), entry.get("owner", "")),
        )


def list_handbook_entries(kind: str | None = None,
                          role: str = ROLE_NEWCOMER) -> list[dict]:
    query = (
        "SELECT entry_id, kind, title, question, answer, steps, links, keywords, "
        "scope, visibility, owner FROM handbook_entries WHERE visibility = ANY(%s)"
    )
    params: list = [visible_levels(role)]
    if kind:
        query += " AND kind = %s"
        params.append(kind)
    query += " ORDER BY kind, entry_id"
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {"entry_id": row[0], "kind": row[1], "title": row[2], "question": row[3],
         "answer": row[4], "steps": row[5], "links": row[6], "keywords": row[7],
         "scope": row[8], "visibility": row[9], "owner": row[10]}
        for row in rows
    ]


def match_faq(question: str, entries: list[dict], min_score: int = 2) -> dict | None:
    """按关键词匹配 FAQ：命中关键词越多越优先，同样多时取关键词更少的（更具体）。

    min_score 默认 2：只命中一个关键词就直连风险太大，比如「电脑连不上网」
    会命中「申请电脑」这条 FAQ。要求至少两个关键词，误命中会明显减少。
    纯函数，方便单测；数据由 find_faq 从库里取。
    """
    text = "".join(str(question).split()).lower()
    if not text:
        return None

    best, best_score, best_keyword_count = None, 0, 10**6
    for entry in entries:
        keywords = [piece.strip() for piece in (entry.get("keywords") or "").split("|")
                    if piece.strip()]
        score = sum(1 for keyword in keywords if keyword.lower() in text)
        if score < min_score:
            continue
        if score > best_score or (score == best_score and len(keywords) < best_keyword_count):
            best, best_score, best_keyword_count = entry, score, len(keywords)
    return best


def find_faq(question: str, role: str = ROLE_NEWCOMER) -> dict | None:
    """在当前角色可见的 FAQ 里找一条最匹配的，用于提问时直连返回。"""
    return match_faq(question, list_handbook_entries(kind="faq", role=role))


def find_process(name: str, role: str = ROLE_NEWCOMER) -> dict | None:
    """按名称或关键词找一条流程。

    这里是模型显式调用工具来查流程，只命中一个关键词也可以接受，
    所以门槛比 FAQ 直连低（FAQ 自动作答必须更保守）。
    """
    return match_faq(name, list_handbook_entries(kind="process", role=role), min_score=1)


def employee_label(employee_id: str) -> str:
    """把员工 ID 换成「姓名（部门）」；找不到就返回原始 ID。"""
    if not employee_id:
        return ""
    with connect() as connection:
        row = connection.execute(
            "SELECT name, department FROM employees WHERE employee_id = %s",
            (employee_id,),
        ).fetchone()
    return f"{row[0]}（{row[1]}）" if row else employee_id


def create_user(username: str, display_name: str, role: str, password_hash: str,
                department: str = "") -> None:
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    with connect() as connection:
        connection.execute(
            "INSERT INTO users (username, display_name, role, department, password_hash) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name, "
            "role = EXCLUDED.role, department = EXCLUDED.department, "
            "password_hash = EXCLUDED.password_hash",
            (username, display_name, role, department, password_hash),
        )


def get_user(username: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT username, display_name, role, department, password_hash "
            "FROM users WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return {"username": row[0], "display_name": row[1], "role": row[2],
            "department": row[3], "password_hash": row[4]}


def list_users() -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT username, display_name, role, department FROM users "
            "ORDER BY role, username"
        ).fetchall()
    return [{"username": r[0], "display_name": r[1], "role": r[2], "department": r[3]}
            for r in rows]


def save_token(token: str, username: str) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO auth_tokens (token, username) VALUES (%s, %s)", (token, username)
        )


def get_token_user(token: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT u.username, u.display_name, u.role, u.department "
            "FROM auth_tokens t JOIN users u ON u.username = t.username WHERE t.token = %s",
            (token,),
        ).fetchone()
    if row is None:
        return None
    return {"username": row[0], "display_name": row[1], "role": row[2], "department": row[3]}


def delete_token(token: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM auth_tokens WHERE token = %s", (token,))


def record_qa_turn(session_id: str, question: str, answer: str, evidence: str,
                   source_count: int = 0, tool_count: int = 0,
                   handoff: bool = False, username: str = "",
                   role: str = ROLE_NEWCOMER,
                   files: list[dict] | None = None) -> int:
    """记一轮问答，返回这轮的编号——反馈和盲区清单都挂在它上面。"""
    with connect() as connection:
        row = connection.execute(
            "INSERT INTO qa_turns (session_id, username, role, question, answer, "
            "evidence, source_count, tool_count, handoff, files) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (session_id, username, role, question, answer, evidence,
             source_count, tool_count, handoff, Jsonb(files or [])),
        ).fetchone()
    return int(row[0])


def set_feedback(turn_id: int, rating: int, comment: str = "") -> bool:
    """给某轮回答打评价：1=有用，-1=没用。允许改评价（同一轮只留最后一次）。"""
    if rating not in (1, -1):
        raise ValueError("评价只能是 1（有用）或 -1（没用）")
    with connect() as connection:
        changed = connection.execute(
            "UPDATE qa_turns SET feedback = %s, feedback_comment = %s, "
            "feedback_at = now() WHERE id = %s",
            (rating, (comment or "").strip()[:500], turn_id),
        ).rowcount
    return bool(changed)


def get_qa_turn(turn_id: int) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT id, session_id, question, answer, evidence, feedback "
            "FROM qa_turns WHERE id = %s",
            (turn_id,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "session_id": row[1], "question": row[2],
            "answer": row[3], "evidence": row[4], "feedback": row[5]}


def list_session_turns(session_id: str) -> list[dict]:
    """某个会话里记过的问答轮次，按时间正序。

    回看历史会话时用它把每轮回答的编号和评价状态还原出来，
    这样刷新页面或切换会话之后仍然能点赞点踩。
    """
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, question, answer, feedback, evidence, files, created_at "
            "FROM qa_turns WHERE session_id = %s ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
    return [
        {"id": row[0], "question": row[1], "answer": row[2], "feedback": row[3],
         "evidence": row[4], "files": row[5] or [], "created_at": row[6].isoformat()}
        for row in rows
    ]


def delete_qa_turns(session_id: str, username: str = "") -> int:
    """删除某个会话的问答留痕（删会话要连问答原文一起删，不能只删聊天文件）。

    未登录的匿名会话只删 username 为空的记录；登录用户只删自己的记录。
    """
    with connect() as connection:
        removed = connection.execute(
            "DELETE FROM qa_turns WHERE session_id = %s AND username = %s",
            (session_id, username or ""),
        ).rowcount
    return removed or 0


def feedback_stats(days: int = 30) -> dict:
    """近期满意度概览：好评率、有多少轮是没有任何依据凭空答的。"""
    with connect() as connection:
        row = connection.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE feedback = 1),
                   count(*) FILTER (WHERE feedback = -1),
                   count(*) FILTER (WHERE evidence = '' AND files = '[]'::jsonb),
                   count(*) FILTER (WHERE handoff)
            FROM qa_turns
            WHERE created_at >= now() - make_interval(days => %s)
            """,
            (days,),
        ).fetchone()
    turns, positive, negative, unsupported, handoffs = row
    rated = positive + negative
    return {
        "days": days,
        "turns": turns,
        "positive": positive,
        "negative": negative,
        "rated": rated,
        "satisfaction": round(positive / rated, 3) if rated else None,
        "unsupported": unsupported,
        "handoffs": handoffs,
    }


def list_feedback_entries(days: int = 30, rating: int = -1,
                          limit: int = 50) -> list[dict]:
    """带评论的反馈明细，默认只看「没用」，用来定位具体哪句回答不达标。"""
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, created_at, username, role, question, answer,
                   feedback, feedback_comment, evidence
            FROM qa_turns
            WHERE created_at >= now() - make_interval(days => %s)
              AND feedback = %s
            ORDER BY feedback_at DESC NULLS LAST
            LIMIT %s
            """,
            (days, rating, limit),
        ).fetchall()
    return [
        {
            "id": row[0], "created_at": row[1].isoformat(), "username": row[2],
            "role": row[3], "question": row[4], "answer": row[5],
            "rating": row[6], "comment": row[7], "evidence": row[8],
        }
        for row in rows
    ]


def list_blind_spots(days: int = 30, limit: int = 50) -> list[dict]:
    """知识盲区清单：答不上来的、或者被点了「没用」的问题，按出现次数排序。

    判定口径写在这里，方便面试时解释"盲区是怎么定义的"：
      - evidence 为空且没有文件附件：这轮回答既没有命中 FAQ/流程/员工卡片，
        也没有检索到资料、没有搜到钉盘文件（纯靠模型常识答的，最容易出错）
      - handoff：直接转人工了，说明知识库接不住
      - feedback = -1：用户明确说没用
    """
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT question,
                   count(*) AS times,
                   max(created_at) AS last_seen,
                   count(*) FILTER (WHERE feedback = -1) AS negative,
                   count(*) FILTER (WHERE handoff) AS handoffs,
                   (array_agg(answer ORDER BY created_at DESC))[1] AS last_answer,
                   (array_agg(evidence ORDER BY created_at DESC))[1] AS last_evidence
            FROM qa_turns
            WHERE created_at >= now() - make_interval(days => %s)
              AND evidence NOT LIKE '%%permission_denied%%'
              AND ((evidence = '' AND files = '[]'::jsonb) OR handoff OR feedback = -1)
            GROUP BY question
            ORDER BY times DESC, last_seen DESC
            LIMIT %s
            """,
            (days, limit),
        ).fetchall()
    return [
        {
            "question": row[0], "times": row[1], "last_seen": row[2].isoformat(),
            "negative": row[3], "handoffs": row[4],
            "last_answer": row[5], "last_evidence": row[6],
        }
        for row in rows
    ]


def log_audit(action: str, username: str = "", role: str = "", target: str = "",
              detail: dict | None = None, status: str = "ok") -> None:
    """写一条审计日志。审计失败不能影响主流程，所以异常直接吞掉并只记警告。"""
    try:
        with connect() as connection:
            connection.execute(
                "INSERT INTO audit_logs (username, role, action, target, detail, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (username or "", role or "", action, target or "",
                 Jsonb(detail or {}), status),
            )
    except Exception:  # noqa: BLE001 - 审计是旁路，不能让它拖垮业务请求
        logger.warning("审计日志写入失败：%s", action, exc_info=True)


def list_audit_logs(limit: int = 100, action: str | None = None,
                    username: str | None = None) -> list[dict]:
    conditions, params = [], []
    if action:
        conditions.append("action = %s")
        params.append(action)
    if username:
        conditions.append("username = %s")
        params.append(username)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    with connect() as connection:
        rows = connection.execute(
            f"SELECT id, created_at, username, role, action, target, detail, status "
            f"FROM audit_logs {where} ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        ).fetchall()
    return [
        {
            "id": row[0], "created_at": row[1].isoformat(), "username": row[2],
            "role": row[3], "action": row[4], "target": row[5],
            "detail": row[6] or {}, "status": row[7],
        }
        for row in rows
    ]


if __name__ == "__main__":
    init_database()
    print("PostgreSQL vector schema ready")
