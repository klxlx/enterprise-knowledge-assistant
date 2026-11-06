"""Split and embed knowledge files, then commit them atomically to PostgreSQL."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from langchain_community.embeddings import DashScopeEmbeddings

import config_data as config
from chunking import split_document
from database import (
    check_embedding_configuration, document_exists, insert_document, normalize_scope,
)


class KnowledgeBaseService:
    def __init__(self, embedding=None):
        check_embedding_configuration()
        self.embedding = embedding if embedding is not None else DashScopeEmbeddings(
            model=config.embedding_model_name
        )

    def upload_by_str(self, data: str, filename: str) -> str:
        return self.upload(data, filename)

    def upload(self, data: str, filename: str, scope=None, visibility: str = "all",
               source_type: str = "upload", source_key=None,
               effective_from: str | None = None,
               expires_at: str | None = None,
               department: str | None = None) -> str:
        """写入一份文档。scope 为空时按通用资料处理。

        effective_from / expires_at 留空时按「今天生效、长期有效」处理。
        手动上传（没有 source_key）视为「同名同范围资料的新版本」，旧版本自动下线；
        外部数据源同步有自己的 source_key 版本逻辑。
        """
        if not data.strip():
            raise ValueError("文件内容不能为空")
        document_hash = hashlib.sha256(data.encode("utf-8")).hexdigest()
        if document_exists(document_hash):
            return "[跳过]，内容已存在知识库中"

        chunks = split_document(data, Path(filename).stem)
        if not chunks:
            raise ValueError("文件内容不能为空")

        # 带章节前缀的文本用于向量化（检索更准），更完整的上下文文本入库供模型阅读。
        vectors = []
        for offset in range(0, len(chunks), 10):
            batch = chunks[offset:offset + 10]
            embedded = self.embedding.embed_documents(
                [chunk.embed_text for chunk in batch]
            )
            if len(embedded) != len(batch):
                raise ValueError("嵌入模型返回数量与文本片段数量不一致")
            vectors.extend(embedded)

        metadata = {
            "source": filename,
            "create_time": datetime.now(timezone.utc).isoformat(),
            "embedding_model": config.embedding_model_name,
        }
        inserted = insert_document(
            document_hash, filename,
            [chunk.context_text for chunk in chunks], vectors, metadata,
            scope=scope, visibility=visibility,
            source_type=source_type, source_key=source_key,
            effective_from=effective_from, expires_at=expires_at,
            replace_existing=source_key is None,
            department=department,
        )
        if not inserted:
            return "[跳过]，内容已存在知识库中"
        model = normalize_scope(scope)
        return (f"[成功]内容已经成功载入向量库"
                f"（适用范围：{model}，部门：{department or '通用'}）")

    def upload_text(self, data: str, filename: str) -> str:
        return self.upload_by_str(data, filename)
