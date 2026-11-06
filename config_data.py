import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
DATA_ROOT = Path(os.environ.get("RAG_DATA_DIR", str(PROJECT_ROOT)))

# PostgreSQL + pgvector. Schema initialization is a separate deployment step.
database_url = os.environ.get("DATABASE_URL", "")
embedding_dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
if not 1 <= embedding_dimensions <= 2000:
    raise ValueError("EMBEDDING_DIMENSIONS must be between 1 and 2000")

embedding_model_name = os.environ.get("EMBEDDING_MODEL", "text-embedding-v4")
chat_model_name = os.environ.get("CHAT_MODEL", "deepseek-v4-pro")

# DashScope 接口地址与密钥
dashscope_api_key = os.environ.get("DASHSCOPE_API_KEY", "")
dashscope_base_url = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"
).rstrip("/")

# 分块策略：先按标题切章节，再在章节内部切到 chunk_size；
# 每块都带章节前缀参与向量化（提升检索命中率），同时按 parent_chars
# 保留更完整的上下文交给模型（小块检索、大块喂模型）。
chunk_size = int(os.environ.get("RAG_CHUNK_SIZE", "500"))
chunk_overlap = int(os.environ.get("RAG_CHUNK_OVERLAP", "80"))
parent_chars = int(os.environ.get("RAG_PARENT_CHARS", "1200"))
separators = ["\n\n", "\n", "。", "；", "！", "？", ".", "!", "?", " ", ""]
max_spliter_char_number = parent_chars  # 兼容旧引用

# 检索参数：先按向量相似度召回 retrieval_k 条候选，再用 score_threshold 过滤掉不相关的。
# score_threshold 是相关度（0~1，1 表示完全一致），值越高越严格。
retrieval_k = int(os.environ.get("RAG_RETRIEVAL_K", "8"))
score_threshold = float(os.environ.get("RAG_SCORE_THRESHOLD", "0.35"))
# 同一个文档最多贡献几条候选：长手册的片段很多，不加限制会把其它资料挤出候选集
max_chunks_per_source = int(os.environ.get("RAG_MAX_CHUNKS_PER_SOURCE", "3"))

# 重排序：对向量召回的候选做精排，保留 rerank_top_n 条，并按 rerank_threshold 过滤。
rerank_enabled = os.environ.get("RAG_RERANK_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
rerank_model_name = os.environ.get("RAG_RERANK_MODEL", "qwen3.7-text-rerank")
rerank_top_n = int(os.environ.get("RAG_RERANK_TOP_N", "3"))
rerank_threshold = float(os.environ.get("RAG_RERANK_THRESHOLD", "0.3"))

# 图片与扫描件的文字识别
ocr_model_name = os.environ.get("RAG_OCR_MODEL", "qwen-vl-ocr")

# 会话历史：只把最近 history_keep_turns 轮原文交给模型，更早的对话压缩成摘要。
history_keep_turns = int(os.environ.get("RAG_HISTORY_KEEP_TURNS", "4"))
history_summary_after_messages = int(os.environ.get("RAG_HISTORY_SUMMARY_AFTER", "10"))
history_summary_trigger_messages = int(os.environ.get("RAG_HISTORY_SUMMARY_TRIGGER", "4"))
summary_model_name = os.environ.get("RAG_SUMMARY_MODEL", "qwen-flash")

# 推理型模型（如 deepseek-v4-pro）会先思考再输出，首字要等几秒到几十秒。
# 关掉思考后首字约 1 秒，代价是不再展示推理过程。
disable_thinking = os.environ.get("RAG_DISABLE_THINKING", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# 上传大小上限，默认 10MB
max_upload_bytes = int(os.environ.get("RAG_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

# 工具调用：一轮提问里最多允许模型调用几次工具，超出后强制它直接作答
max_tool_rounds = int(os.environ.get("RAG_MAX_TOOL_ROUNDS", "3"))

# 钉钉文件检索：mock=读本地清单模拟；real=拿到企业应用授权后走开放平台接口。
# index_path 只在 mock 模式用到；app_key/app_secret 只在 real 模式用到。
dingtalk_mode = os.environ.get("DINGTALK_MODE", "mock").strip().lower()
dingtalk_index_path = Path(os.environ.get(
    "DINGTALK_INDEX_PATH", str(PROJECT_ROOT / "mock" / "dingtalk" / "index.json")
))
dingtalk_app_key = os.environ.get("DINGTALK_APP_KEY", "")
dingtalk_app_secret = os.environ.get("DINGTALK_APP_SECRET", "")

session_config = {
    "configurable": {"session_id": "user_001"}
}
