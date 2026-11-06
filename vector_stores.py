"""检索层：pgvector 向量召回 + DashScope 重排序。"""

import logging
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache

# 必须在创建 PGEngine（后台事件循环）之前执行，见模块内说明。
import platform_setup  # noqa: F401

from langchain_postgres import PGEngine, PGVectorStore

import config_data as config
from database import (
    GENERIC_MODEL, ROLE_NEWCOMER, VISIBILITY_ALL,
    check_embedding_configuration, normalize_scope,
)
from permissions import visibility_clauses
from rerank import RerankError, rerank

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_pg_engine() -> PGEngine:
    if not config.database_url:
        raise ValueError("DATABASE_URL must be configured")
    return PGEngine.from_connection_string(
        config.database_url, pool_size=2, max_overflow=2,
        pool_timeout=10, pool_pre_ping=True, connect_args={"connect_timeout": 5},
    )


@dataclass(frozen=True)
class RetrievedChunk:
    """一条检索结果。index 是 1 开始编号，供答案里的【编号】引用。"""

    index: int
    content: str
    source: str
    chunk_index: int
    score: float
    reranked: bool = False
    scope: str = GENERIC_MODEL
    visibility: str = VISIBILITY_ALL
    department: str = ""
    effective_from: str = ""
    expires_at: str = ""

    def as_source(self) -> dict:
        """返回给前端的来源信息，不含正文，避免响应体过大。"""
        return {
            "index": self.index,
            "source": self.source,
            "chunk": self.chunk_index,
            "score": round(self.score, 3),
            "reranked": self.reranked,
            "scope": self.scope,
            "visibility": self.visibility,
            "department": self.department,
            "effective_from": self.effective_from,
            "expires_at": self.expires_at,
        }


class VectorStoreService:
    def __init__(self, embedding):
        self.embedding = embedding
        check_embedding_configuration()
        self.vector_store = PGVectorStore.create_sync(
            engine=get_pg_engine(), table_name="rag_chunks",
            embedding_service=embedding, id_column="id", content_column="content",
            embedding_column="embedding",
            metadata_columns=["document_hash", "scope", "visibility", "department",
                              "is_current", "effective_from", "expires_at"],
            metadata_json_column="metadata",
        )

    @staticmethod
    def _build_filter(scope: str | None, role: str, department: str = "") -> dict:
        """只检索当前版本。

        scope 为 None：全部适用范围（用户没说型号/部门时的兜底）
        scope 为「通用」：只检索通用资料
        scope 为具体值：该值 + 通用资料

        同时按角色过滤可见性：新人只能看 all，员工还能看 staff。
        """
        conditions = [
            {"is_current": True},
            # 部门维度：全员公开 + 自己部门 + （主管）仅主管资料；敏感资料不进问答
            {"$or": visibility_clauses(role, department, include_sensitive=False)},
            # 失效资料不再参与检索
            {"expires_at": {"$gte": date.today().isoformat()}},
        ]
        if scope == GENERIC_MODEL:
            conditions.append({"scope": GENERIC_MODEL})
        elif scope:
            # 用包含匹配：一份资料可能同时标了多个适用范围（如「DEEBOT X5 MAX, DEEBOT X5 PRO」），
            # 用户说其中任意一个都应命中；说族名（如「DEEBOT X5」）也能命中。
            pattern = "%" + scope.replace("%", "") + "%"
            conditions.append({"$or": [
                {"scope": {"$ilike": pattern}},
                {"scope": GENERIC_MODEL},
            ]})
        return {"$and": conditions}

    def _recall(self, query: str, scope: str | None, role: str,
                department: str = "") -> list[RetrievedChunk]:
        docs_and_scores = self.vector_store.similarity_search_with_relevance_scores(
            query, k=config.retrieval_k, score_threshold=config.score_threshold,
            filter=self._build_filter(scope, role, department),
        )
        candidates = []
        per_source: dict[str, int] = {}
        for document, score in docs_and_scores:
            metadata = document.metadata or {}
            source = str(metadata.get("source", "未知来源"))
            # 长手册的片段很多，限制单份文档的候选数，避免把其它资料挤出候选集
            if per_source.get(source, 0) >= config.max_chunks_per_source:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            candidates.append(RetrievedChunk(
                index=len(candidates) + 1,
                content=document.page_content,
                source=source,
                chunk_index=int(metadata.get("chunk_index", 0)),
                score=float(score),
                scope=str(metadata.get("scope") or GENERIC_MODEL),
                visibility=str(metadata.get("visibility") or VISIBILITY_ALL),
                department=str(metadata.get("department") or ""),
                effective_from=str(metadata.get("effective_from") or ""),
                expires_at=str(metadata.get("expires_at") or ""),
            ))
        return candidates

    def search(self, query: str, scope: str | None = None,
               role: str = ROLE_NEWCOMER,
               department: str = "") -> list[RetrievedChunk]:
        """向量召回 → 阈值过滤 → 重排序，编号从 1 开始。

        指定适用范围时只检索该值和通用资料；如果没有命中任何资料，
        会退回全库检索，由调用方决定怎么向用户说明。
        """
        normalized = normalize_scope(scope) if scope else None
        candidates = self._recall(query, normalized, role, department)
        if not candidates and normalized and normalized != GENERIC_MODEL:
            # 没有专属资料时只退回通用资料，绝不带出别的型号/部门的结论
            candidates = self._recall(query, GENERIC_MODEL, role, department)

        # 只有 1 条候选时没有排序可言，省掉一次重排序调用。
        if len(candidates) < 2 or not config.rerank_enabled:
            return candidates
        return _rerank_candidates(query, candidates)


def _rerank_candidates(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """精排失败时退回向量顺序，保证检索链路不中断。"""
    try:
        ranked = rerank(query, [item.content for item in candidates], config.rerank_top_n)
    except RerankError as exc:
        logger.warning("重排序不可用，退回向量排序：%s", exc)
        return candidates[:config.rerank_top_n]

    results: list[RetrievedChunk] = []
    for position, score in ranked:
        if not 0 <= position < len(candidates) or score < config.rerank_threshold:
            continue
        results.append(replace(
            candidates[position],
            index=len(results) + 1,
            score=score,
            reranked=True,
        ))
    return results
