"""Unit tests for the heading-aware chunker."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from chunker import chunk_markdown, _get_heading_text


SIMPLE_MD = """### 3.1 2016年武将

> 基础武将

| ID | Name | Quality |
|:--:|------|:-------:|
| 11 | 吕布 | 传说 |
| 7  | 大乔 | 传说 |
| 71 | 司马懿 | 传说 |
"""

CRLF_MD = "### 3.1 2016年武将\r\n\r\n> 基础武将\r\n\r\n| ID | Name | Quality |\r\n|:--:|------|:-------:|\r\n| 11 | 吕布 | 传说 |\r\n| 7  | 大乔 | 传说 |\r\n| 71 | 司马懿 | 传说 |\r\n"

MULTI_SECTION = """## 三、武将列表

### 3.1 2016年武将（开服前）

> 2016年上线

| ID | Name |
|:--:|------|
| 11 | 吕布 |
| 7  | 大乔 |

### 3.2 2017年武将

> 周年庆

| ID | Name |
|:--:|------|
| 80 | 姜维 |
"""


def test_empty_input():
    assert chunk_markdown("") == []
    assert chunk_markdown("   ") == []


def test_small_document_stays_together():
    """文档足够小时，标题+内容应在同一切片中"""
    chunks = chunk_markdown(SIMPLE_MD, chunk_size=2048)
    assert len(chunks) == 1
    assert "### 3.1 2016年武将" in chunks[0]
    assert "吕布" in chunks[0]
    assert "大乔" in chunks[0]


def test_heading_context_on_split():
    """强制拆分后，续片应有标题备注"""
    chunks = chunk_markdown(SIMPLE_MD, chunk_size=80)
    # 至少应有首片 + 若干续片
    assert len(chunks) > 1
    # 续片应有备注
    has_note = any("2016年武将" in c for c in chunks[1:])
    assert has_note, f"续片应有标题备注，got: {[c[:60] for c in chunks]}"


def test_crlf_line_endings():
    """Windows \\r\\n 应被正确处理"""
    chunks = chunk_markdown(CRLF_MD, chunk_size=2048)
    assert len(chunks) >= 1
    full = " ".join(chunks)
    assert "吕布" in full


def test_multiple_sections_separate():
    """不同 ### 标题的 section 应在不同切片中"""
    chunks = chunk_markdown(MULTI_SECTION, chunk_size=512)
    chunk_text = [c for c in chunks]
    # 2016年武将 和 2017年武将 不应混在同一片中
    for i, c in enumerate(chunk_text):
        if "2016" in c and "2017" in c:
            assert False, f"Chunk {i} contains both 2016 and 2017 sections"


def test_heading_text_extraction():
    """_get_heading_text 应去掉 ### 前缀"""
    assert _get_heading_text("### 3.1 2016年武将（开服前）") == "3.1 2016年武将"
    assert _get_heading_text("## 三、武将列表") == "三、武将列表"
    assert _get_heading_text("") == ""


def test_no_duplicate_context_notes():
    """续片备注不应重复添加"""
    chunks = chunk_markdown(SIMPLE_MD, chunk_size=70)
    for c in chunks:
        lines = c.splitlines()
        note_lines = [l for l in lines if l.startswith("> ")]
        assert len(note_lines) <= 2, f"Too many context notes in chunk: {c[:100]}"


def test_tagged_chunks_contain_keywords():
    """所有切片（含续片）应包含可搜索的关键词"""
    chunks = chunk_markdown(SIMPLE_MD, chunk_size=80)
    all_text = "\n".join(chunks)
    assert "2016年武将" in all_text  # heading should appear somewhere
    assert "吕布" in all_text
    assert "大乔" in all_text


def test_long_unbroken_paragraph_preserves_tail():
    """没有自然断句的超长段落也不能丢失 chunk_size 之后的内容。"""
    content = "中" * 1500 + "尾"

    chunks = chunk_markdown(content, chunk_size=512, overlap=50)

    assert len(chunks) >= 3
    assert "尾" in "".join(chunks)
