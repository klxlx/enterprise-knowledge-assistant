"""把上传的文件解析成纯文本。

支持 TXT / Markdown / PDF / Word(.docx) / 图片。
PDF 走文本层提取，扫描件会提示改用图片；图片走 DashScope 的 OCR 模型识别。
"""

import base64
import io
import json
import urllib.error
import urllib.request
from pathlib import PurePosixPath

import config_data as config

TEXT_SUFFIXES = (".txt", ".md", ".markdown")
PDF_SUFFIXES = (".pdf",)
WORD_SUFFIXES = (".docx",)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
SUPPORTED_SUFFIXES = TEXT_SUFFIXES + PDF_SUFFIXES + WORD_SUFFIXES + IMAGE_SUFFIXES

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# PDF 文本层短于这个长度就当作扫描件
MIN_PDF_TEXT_CHARS = 50


class UnsupportedFileType(ValueError):
    """后缀不在支持范围内。"""


class ExtractionFailed(RuntimeError):
    """文件类型支持，但内容解析不了（扫描件、加密文档、图片无文字等）。"""


def suffix_of(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def supported_hint() -> str:
    return "TXT、Markdown、PDF、Word(.docx)、图片(png/jpg/webp/bmp)"


def extract_text(data: bytes, filename: str) -> str:
    suffix = suffix_of(filename)
    if suffix in TEXT_SUFFIXES:
        return _decode_text(data)
    if suffix in PDF_SUFFIXES:
        return _from_pdf(data)
    if suffix in WORD_SUFFIXES:
        return _from_docx(data)
    if suffix in IMAGE_SUFFIXES:
        return _from_image(data, suffix)
    raise UnsupportedFileType(
        f"暂不支持 {suffix or '该类型'} 文件，支持：{supported_hint()}"
    )


def _decode_text(data: bytes) -> str:
    """TXT / Markdown：优先按 UTF-8 解码，兼容 GBK 系编码。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionFailed("文本文件编码无法识别，请另存为 UTF-8 后重试")


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ExtractionFailed(f"PDF 解析失败：{exc}") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if len(text) < MIN_PDF_TEXT_CHARS:
        raise ExtractionFailed(
            "这个 PDF 没有可提取的文字层，可能是扫描件；请导成图片上传，让系统走 OCR 识别"
        )
    return text


def _from_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionFailed(f"Word 文档解析失败：{exc}") from exc

    # Word 的标题样式还原成 Markdown 标题，后面的分块器才能按章节切分。
    parts = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").strip().lower()
        heading_level = _heading_level(style)
        parts.append(f"{'#' * heading_level} {text}" if heading_level else text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    if not parts:
        raise ExtractionFailed("Word 文档里没有可提取的文字")
    return "\n".join(parts)


def _heading_level(style_name: str) -> int:
    """把 Word 的「Heading 1」或中文「标题 1」样式换算成 Markdown 标题层级。"""
    if not (style_name.startswith("heading") or style_name.startswith("标题")):
        return 0
    digits = "".join(char for char in style_name if char.isdigit())
    level = int(digits) if digits else 2
    return max(1, min(6, level))


def _from_image(data: bytes, suffix: str) -> str:
    if not config.dashscope_api_key:
        raise ExtractionFailed("未配置 DASHSCOPE_API_KEY，无法识别图片文字")

    mime = IMAGE_MIME_TYPES.get(suffix, "image/png")
    encoded = base64.b64encode(data).decode()
    payload = {
        "model": config.ocr_model_name,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                {"type": "text",
                 "text": "请把图片中的文字原样提取出来，只输出文字内容，不要添加解释。"},
            ],
        }],
        "max_tokens": 4000,
    }
    request = urllib.request.Request(
        f"{config.dashscope_base_url}/compatible-mode/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {config.dashscope_api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise ExtractionFailed(f"图片识别失败（HTTP {exc.code}）：{detail}") from exc
    except Exception as exc:
        raise ExtractionFailed(f"图片识别失败：{exc}") from exc

    message = (result.get("choices") or [{}])[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        # 部分模型返回结构化内容，这里把文本片段拼起来
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not str(content).strip():
        raise ExtractionFailed("图片里没有识别到文字")
    return str(content)
