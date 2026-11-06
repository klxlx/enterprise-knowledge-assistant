"""文件解析的单元测试（不访问网络，图片 OCR 单独人工验证）。"""

import io

import pytest

import documents


def test_utf8_text_is_decoded():
    data = "滤网清洗后要晾干。".encode("utf-8")
    assert documents.extract_text(data, "说明.txt") == "滤网清洗后要晾干。"


def test_gbk_text_is_decoded():
    data = "滤网清洗后要晾干。".encode("gb18030")
    assert documents.extract_text(data, "说明.txt") == "滤网清洗后要晾干。"


def test_markdown_is_treated_as_text():
    assert documents.extract_text("# 标题".encode("utf-8"), "说明.md") == "# 标题"


def test_unsupported_suffix_raises():
    with pytest.raises(documents.UnsupportedFileType):
        documents.extract_text(b"x", "表格.xlsx")


def test_broken_pdf_reports_extraction_failure():
    with pytest.raises(documents.ExtractionFailed):
        documents.extract_text(b"%PDF-1.4 broken payload", "坏文件.pdf")


def test_docx_paragraphs_and_tables_are_extracted():
    import docx

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_heading("滤网保养", level=1)
    document.add_paragraph("滤网清洗后必须完全晾干再装回。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "滤网"
    table.cell(0, 1).text = "每周清理"
    document.save(buffer)

    text = documents.extract_text(buffer.getvalue(), "保养.docx")

    assert "# 滤网保养" in text
    assert "滤网清洗后必须完全晾干再装回。" in text
    assert "滤网 | 每周清理" in text


def test_supported_suffixes_cover_common_documents():
    for suffix in (".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        assert suffix in documents.SUPPORTED_SUFFIXES
