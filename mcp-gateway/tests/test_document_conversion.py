import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from document_conversion import DocumentConversionError, convert_uploaded_document, is_supported_document_filename
from chunker import chunk_markdown


def test_csv_conversion_preserves_headers_for_each_record():
    content = convert_uploaded_document(
        "inventory.CSV",
        "\ufeff商品,库存,说明\n键盘,12,办公用\n鼠标,8,无线\n".encode("utf-8"),
    )

    assert content.startswith("# inventory\n")
    assert "## CSV 数据记录" in content
    assert "- 行号: 1\n  - 商品: 键盘\n  - 库存: 12\n  - 说明: 办公用" in content
    assert "- 行号: 2\n  - 商品: 鼠标\n  - 库存: 8\n  - 说明: 无线" in content


def test_csv_conversion_detects_semicolon_and_gb18030():
    content = convert_uploaded_document(
        "stock.csv",
        "名称;数量\n键盘;12\n".encode("gb18030"),
    )

    assert "- 名称: 键盘" in content
    assert "- 数量: 12" in content


def test_csv_conversion_decodes_utf16_with_bom():
    content = convert_uploaded_document(
        "stock.csv",
        "名称,数量\n键盘,12\n".encode("utf-16"),
    )

    assert "- 名称: 键盘" in content


def test_csv_without_header_keeps_first_record_with_synthetic_columns():
    content = convert_uploaded_document("rows.csv", b"1,Alice\n2,Bob\n")

    assert "- 列1: 1" in content
    assert "- 列2: Alice" in content
    assert "- 列1: 2" in content


def test_empty_csv_is_rejected():
    with pytest.raises(DocumentConversionError, match="没有可导入的数据行"):
        convert_uploaded_document("empty.csv", "名称,数量\n".encode("utf-8"))


def test_document_extension_support_is_case_insensitive():
    assert is_supported_document_filename("guide.MD")
    assert is_supported_document_filename("inventory.CsV")
    assert not is_supported_document_filename("report.xlsx")


def test_csv_records_keep_column_labels_after_chunking():
    content = convert_uploaded_document(
        "inventory.csv",
        "商品,库存\n键盘,12\n鼠标,8\n".encode("utf-8"),
    )

    chunks = chunk_markdown(content, chunk_size=70, overlap=0)

    assert any("- 商品: 键盘" in chunk and "- 库存: 12" in chunk for chunk in chunks)
    assert any("- 商品: 鼠标" in chunk and "- 库存: 8" in chunk for chunk in chunks)


def test_upload_page_exposes_csv_selection():
    template = (Path(__file__).parent.parent / "src" / "admin" / "templates" / "upload.html").read_text(encoding="utf-8")

    assert 'accept=".md,.csv,.zip,.tar.gz,.tgz"' in template
    assert r"/\.(md|csv)$/i.test(f.name)" in template
