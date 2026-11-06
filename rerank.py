"""调用 DashScope 的文本重排序接口，对向量召回的候选做精排。

向量检索擅长"意思相近"，重排序模型擅长判断"到底有没有回答这个问题"，
两者配合可以显著提升最终喂给模型的片段质量。
"""

import json
import urllib.error
import urllib.request

import config_data as config


class RerankError(RuntimeError):
    """重排序不可用，调用方应退回向量排序。"""


def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    """返回 (原始下标, 相关度) 列表，按相关度从高到低。"""
    if not documents:
        return []
    if not config.dashscope_api_key:
        raise RerankError("未配置 DASHSCOPE_API_KEY")

    payload = {
        "model": config.rerank_model_name,
        "input": {"query": query, "documents": documents},
        "parameters": {
            "return_documents": False,
            "top_n": min(top_n, len(documents)),
        },
    }
    request = urllib.request.Request(
        f"{config.dashscope_base_url}/api/v1/services/rerank/text-rerank/text-rerank",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {config.dashscope_api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RerankError(f"HTTP {exc.code} {detail}") from exc
    except Exception as exc:
        raise RerankError(f"{type(exc).__name__}: {exc}") from exc

    rows = (result.get("output") or {}).get("results") or []
    ranked = [(int(row["index"]), float(row["relevance_score"])) for row in rows]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked
