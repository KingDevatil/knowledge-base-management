import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from document_metadata import (
    entity_node_id,
    extract_document_header_metadata,
    merge_metadata_values,
)


def test_extract_document_header_metadata_supports_chinese_and_english_labels():
    content = """# Gateway deployment

> 标签：部署、内部服务
Tags: API, 运维
核心实体：MCP Gateway；Chroma | Redis
**Core Entities**: Ollama, #MCP-Gateway

## Architecture

实体：不应从正文解析
"""

    metadata = extract_document_header_metadata(content)

    assert metadata.tags == ["部署", "内部服务", "API", "运维"]
    assert metadata.entities == ["MCP Gateway", "Chroma", "Redis", "Ollama"]


def test_extract_document_header_metadata_accepts_entity_and_core_entity_variants():
    content = """# 模块说明

实体: 用户中心，权限服务
Core Entity：User Service; Access Control
Entity: Audit Log
Tag：架构 | 安全
"""

    metadata = extract_document_header_metadata(content)

    assert metadata.tags == ["架构", "安全"]
    assert metadata.entities == ["用户中心", "权限服务", "User Service", "Access Control", "Audit Log"]


def test_metadata_values_merge_without_case_or_separator_duplicates():
    merged = merge_metadata_values(
        ["部署", "MCP Gateway"],
        ["部署", "mcp-gateway", "检索"],
    )

    assert merged == ["部署", "MCP Gateway", "检索"]


def test_entity_node_id_is_stable_for_case_and_separator_variants():
    assert entity_node_id("MCP Gateway") == entity_node_id("mcp-gateway")
    assert entity_node_id("知识库") != entity_node_id("向量库")
