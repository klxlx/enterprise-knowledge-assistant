"""按文档结构切分文本。

先按 Markdown 标题（#、##、###…）或【小标题】切出章节，再在章节内部按段落和中文句号
切到目标长度。每个片段都带“文档标题 · 章节路径”前缀参与向量化，检索时更容易分辨主题；
同时按 parent_chars 保留更大范围的上下文交给模型，做到小块检索、大块喂模型。
"""

import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config_data as config

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")
_BRACKET_HEADING_PATTERN = re.compile(r"^【(.{1,30})】$")


@dataclass(frozen=True)
class Chunk:
    index: int
    embed_text: str    # 参与向量化，带章节前缀
    context_text: str  # 交给模型的上下文，比 embed_text 更完整
    section: str       # 章节路径


def split_document(text: str, doc_title: str) -> list[Chunk]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        separators=config.separators,
        length_function=len,
    )

    chunks: list[Chunk] = []
    for section, body in _split_sections(text):
        pieces = [piece for piece in splitter.split_text(body) if piece.strip()]
        if not pieces:
            continue
        prefix = f"{doc_title} · {section}" if section else doc_title
        for position, piece in enumerate(pieces):
            window = _window(pieces, position)
            chunks.append(Chunk(
                index=len(chunks),
                embed_text=f"{prefix}\n{piece}",
                context_text=f"{prefix}\n{window}",
                section=section,
            ))
    return chunks


def _split_sections(text: str) -> list[tuple[str, str]]:
    """返回 (章节路径, 正文) 列表；整篇没有标题时作为一节处理。"""
    sections: list[tuple[str, str]] = []
    heading_path: list[str] = []
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((" / ".join(part for part in heading_path if part), body))
        body_lines.clear()

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        markdown_heading = _HEADING_PATTERN.match(stripped)
        bracket_heading = _BRACKET_HEADING_PATTERN.match(stripped)
        if markdown_heading or bracket_heading:
            flush()
            if markdown_heading:
                level = len(markdown_heading.group(1))
                title = markdown_heading.group(2).strip()
            else:
                level = 2
                title = bracket_heading.group(1).strip()
            heading_path = [part for part in heading_path[: level - 1]]
            while len(heading_path) < level - 1:
                heading_path.append("")
            heading_path.append(title)
            continue
        body_lines.append(line)

    flush()
    if not sections:
        content = text.strip()
        if content:
            sections.append(("", content))
    return sections


def _window(pieces: list[str], position: int) -> str:
    """把当前片段扩展成尽量接近 parent_chars 的窗口，避免答案横跨两段时被截断。"""
    start = end = position
    size = len(pieces[position])
    while size < config.parent_chars:
        extended = False
        if start > 0 and size + len(pieces[start - 1]) <= config.parent_chars:
            start -= 1
            size += len(pieces[start])
            extended = True
        if end + 1 < len(pieces) and size + len(pieces[end + 1]) <= config.parent_chars:
            end += 1
            size += len(pieces[end])
            extended = True
        if not extended:
            break
    return "\n".join(pieces[start:end + 1])
