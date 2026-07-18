from pathlib import Path

from admin import routes_pages


TEMPLATES = Path(__file__).parent.parent / "src" / "admin" / "templates"


def test_document_markdown_preserves_consecutive_metadata_lines():
    html, _ = routes_pages._render_document_markdown(
        "> 标签：战斗机制、破盾\n> 核心实体：气盾、元素盾"
    )

    assert "标签：战斗机制、破盾<br" in html
    assert "核心实体：气盾、元素盾" in html


def test_editor_preview_preserves_soft_line_breaks():
    template = (TEMPLATES / "document_edit.html").read_text(encoding="utf-8")

    assert "breaks: true" in template
    assert "仅修改所在目录时会快速迁移源文件和索引元数据" in template
    assert 'id="document-edit-form"' in template
    assert 'id="save-document-btn"' in template
    assert "保存中..." in template


def test_document_list_shows_save_success_feedback():
    template = (TEMPLATES / "documents.html").read_text(encoding="utf-8")

    assert "文档已保存" in template
    assert "request.query_params.get('save_status')" in template
    assert 'id="save-success-banner"' in template


def test_graph_sticky_header_uses_opaque_page_header_surface():
    template = (TEMPLATES / "graph.html").read_text(encoding="utf-8")
    header = template.split("</header>", 1)[0]

    assert 'class="page-header sticky top-0' in header
    assert "glass-header" not in header
