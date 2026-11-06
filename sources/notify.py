"""同步结果通知。

钉钉群自定义机器人的 webhook 是**单向**的：只能发消息，收不到群消息。
想接收群里的提问，必须做企业内部应用机器人（需要企业管理员授权）。
"""

import json
import urllib.request


def send_dingtalk_text(webhook_url: str, content: str, timeout: int = 10) -> tuple[bool, str]:
    """往钉钉群推一条文本消息，返回（是否成功, 说明）。"""
    if not webhook_url:
        return False, "未配置 webhook"

    payload = {"msgtype": "text", "text": {"content": content}}
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if result.get("errcode") == 0:
        return True, "ok"
    return False, str(result.get("errmsg") or result)
