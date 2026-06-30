"""Security unit tests: Open Redirect, rate limiting, Pydantic validation, CSRF."""
import sys
import os
import io
import tarfile
import zipfile
import json
from types import SimpleNamespace

# Set DEBUG mode before any project imports to prevent SESSION_SECRET validation
os.environ["DEBUG"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from fastapi import HTTPException

from models import APIKeyInfo, AddDocumentRequest, UpdateDocumentRequest, MAX_CONTENT_LENGTH

# Import the validate function (we need to test it directly)
from admin.routes_api import _validate_redirect_url
from admin.archive_security import ArchiveValidationError, safe_extract_archive, validate_archive_size
from admin.routes_documents_api import _upload_basename
from admin_auth import AdminAuth
from auth import APIKeyAuth
from markdown_security import sanitize_markdown_html
from mcp_auth_context import reset_mcp_api_key_info, set_mcp_api_key_info
from path_permissions import has_path_access, parse_allowed_paths
from server import MCP_TOOL_METADATA, require_mcp_tool_scope
from api_routes import health_check


# ==================== Open Redirect Tests ====================

class TestRedirectValidation:
    """Test _validate_redirect_url() prevents Open Redirect attacks."""

    def test_valid_relative_path(self):
        """Normal relative paths should pass through."""
        assert _validate_redirect_url("/admin/dashboard") == "/admin/dashboard"
        assert _validate_redirect_url("/admin/documents") == "/admin/documents"
        assert _validate_redirect_url("/admin/settings") == "/admin/settings"

    def test_subdir_paths(self):
        """Paths with query strings or subdirectories should pass."""
        assert _validate_redirect_url("/admin/documents?path=foo") == "/admin/documents?path=foo"
        assert _validate_redirect_url("/admin/documents/abc123") == "/admin/documents/abc123"

    def test_empty_next_falls_back_to_default(self):
        """Empty next parameter should use default."""
        assert _validate_redirect_url("") == "/admin/dashboard"

    def test_absolute_url_blocked(self):
        """Absolute URLs (https://evil.com) must be blocked."""
        assert _validate_redirect_url("https://evil.com/phishing") == "/admin/dashboard"

    def test_protocol_relative_url_blocked(self):
        """Protocol-relative URLs (//evil.com) must be blocked."""
        assert _validate_redirect_url("//evil.com/phishing") == "/admin/dashboard"

    def test_javascript_url_blocked(self):
        """JavaScript URLs must be blocked."""
        assert _validate_redirect_url("javascript:alert(1)") == "/admin/dashboard"

    def test_custom_default(self):
        """Custom default should work."""
        assert _validate_redirect_url("", "/admin/settings") == "/admin/settings"
        assert _validate_redirect_url("https://evil.com", "/admin/settings") == "/admin/settings"


# ==================== MCP Scope Tests ====================

def _api_key_info(scope: list[str]) -> APIKeyInfo:
    return APIKeyInfo(
        key_prefix="sk-test",
        applicant="tester",
        scope=scope,
        rate_limit=30,
        status="active",
        duration="7d",
        created_at="2026-01-01T00:00:00Z",
        expires_at="",
    )


class TestMCPScopeGuards:
    """MCP write tools must require write scope, matching the REST API."""

    def test_write_tool_metadata_requires_write_scope(self):
        assert MCP_TOOL_METADATA["add_document"]["required_scope"] == "write"
        assert MCP_TOOL_METADATA["update_document"]["required_scope"] == "write"
        assert MCP_TOOL_METADATA["delete_document"]["required_scope"] == "write"
        assert MCP_TOOL_METADATA["search_knowledge"]["required_scope"] == "read"

    def test_read_key_cannot_call_write_tool(self):
        token = set_mcp_api_key_info(_api_key_info(["read"]))
        try:
            with pytest.raises(HTTPException) as exc:
                require_mcp_tool_scope("add_document")
            assert exc.value.status_code == 403
        finally:
            reset_mcp_api_key_info(token)

    def test_write_key_can_call_write_tool(self):
        token = set_mcp_api_key_info(_api_key_info(["read", "write"]))
        try:
            require_mcp_tool_scope("add_document")
        finally:
            reset_mcp_api_key_info(token)


class TestAPIKeyScopeParsing:
    """Legacy API key scope formats should not break authentication."""

    def test_plain_string_scope_is_accepted_as_single_scope(self):
        auth = APIKeyAuth(redis_client=None, api_key_file="unused")  # type: ignore[arg-type]
        assert auth._parse_scope("read") == ["read"]

    def test_json_encoded_scope_is_accepted(self):
        auth = APIKeyAuth(redis_client=None, api_key_file="unused")  # type: ignore[arg-type]
        assert auth._parse_scope('["read", "write"]') == ["read", "write"]


class TestAPIKeyPathPermissions:
    def test_allowed_paths_parse_json_and_csv(self):
        assert parse_allowed_paths('["team-a/docs", "/team-b"]') == ["team-a/docs", "team-b"]
        assert parse_allowed_paths("team-a/docs, /team-b") == ["team-a/docs", "team-b"]

    def test_all_mode_allows_every_path(self):
        info = _api_key_info(["read"])
        assert has_path_access(info, "any/path")

    def test_restricted_mode_allows_descendants_only(self):
        info = APIKeyInfo(
            key_prefix="sk-test",
            applicant="tester",
            scope=["read"],
            path_mode="restricted",
            allowed_paths=["team-a/docs"],
            created_at="2026-01-01T00:00:00Z",
            expires_at="",
        )
        assert has_path_access(info, "team-a/docs")
        assert has_path_access(info, "team-a/docs/specs")
        assert not has_path_access(info, "team-a/private")


class TestMarkdownSanitizer:
    """Rendered Markdown HTML must not execute scripts."""

    def test_script_tags_and_event_handlers_are_removed(self):
        html = '<h1 id="x">Title</h1><script>alert(1)</script><img src="https://example.com/a.png" onerror="alert(2)">'
        cleaned = sanitize_markdown_html(html)
        assert "<script" not in cleaned.lower()
        assert "onerror" not in cleaned.lower()
        assert '<h1 id="x">Title</h1>' in cleaned
        assert 'src="https://example.com/a.png"' in cleaned

    def test_javascript_links_are_stripped(self):
        cleaned = sanitize_markdown_html('<a href="javascript:alert(1)">bad</a>')
        assert "javascript:" not in cleaned.lower()
        assert ">bad</a>" in cleaned


class TestAdminSessionInvalidation:
    """Password changes should invalidate older session tokens."""

    @pytest.mark.asyncio
    async def test_password_change_invalidates_existing_session(self, tmp_path):
        class Redis:
            async def get(self, key):
                return None

        accounts_file = tmp_path / "accounts.json"
        auth = AdminAuth(Redis(), str(accounts_file), "x" * 32)
        assert auth.ensure_bootstrap_admin("admin", "old-pass")
        token = auth.create_session_token("admin", "super_admin")

        ok, msg = await auth.change_password("admin", "old-pass", "new-pass")
        assert ok, msg

        request = SimpleNamespace(cookies={"session": token})
        with pytest.raises(HTTPException) as exc:
            await auth.verify_session(request)
        assert exc.value.status_code == 401


# ==================== Health Check Tests ====================

class TestHealthCheck:
    """Health endpoint should expose embedding provider status without breaking legacy service status."""

    @pytest.mark.asyncio
    async def test_health_includes_embedding_provider_status(self):
        class Redis:
            async def ping(self):
                return True

        class Chroma:
            def heartbeat(self):
                return True

        class Embedder:
            async def health_check(self):
                return True

            def status(self):
                return [{"name": "fake", "failures": 0, "circuit_open": False, "cached_health": True}]

        class Store:
            def bucket_exists(self, bucket):
                return True

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    redis=Redis(),
                    chroma=Chroma(),
                    embedder=Embedder(),
                    source_store=Store(),
                )
            )
        )

        response = await health_check(request)
        payload = json.loads(response.body)

        assert response.status_code == 200
        assert payload["services"]["ollama"] == "ok"
        assert payload["embedding_providers"][0]["name"] == "fake"


# ==================== Archive Extraction Tests ====================

class TestArchiveSecurity:
    """Uploaded archives must not write outside the temporary extraction directory."""

    def test_safe_zip_extracts_markdown(self, tmp_path):
        archive = tmp_path / "docs.zip"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("guide/readme.md", "# Readme")

        extracted = safe_extract_archive(str(archive), str(extract_dir), "docs.zip")
        assert extracted == 1
        assert (extract_dir / "guide" / "readme.md").read_text(encoding="utf-8") == "# Readme"

    def test_zip_slip_path_is_rejected(self, tmp_path):
        archive = tmp_path / "evil.zip"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../evil.md", "# Evil")

        with pytest.raises(ArchiveValidationError):
            safe_extract_archive(str(archive), str(extract_dir), "evil.zip")
        assert not (tmp_path / "evil.md").exists()

    def test_tar_slip_path_is_rejected(self, tmp_path):
        archive = tmp_path / "evil.tar.gz"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        with tarfile.open(archive, "w:gz") as tf:
            data = b"# Evil"
            info = tarfile.TarInfo("../evil.md")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        with pytest.raises(ArchiveValidationError):
            safe_extract_archive(str(archive), str(extract_dir), "evil.tar.gz")
        assert not (tmp_path / "evil.md").exists()

    def test_tar_symlink_is_rejected(self, tmp_path):
        archive = tmp_path / "symlink.tar.gz"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("link.md")
            info.type = tarfile.SYMTYPE
            info.linkname = "../outside.md"
            tf.addfile(info)

        with pytest.raises(ArchiveValidationError):
            safe_extract_archive(str(archive), str(extract_dir), "symlink.tar.gz")

    def test_archive_size_limit_is_enforced(self):
        with pytest.raises(ArchiveValidationError):
            validate_archive_size(b"x" * (200 * 1024 * 1024 + 1))

    def test_upload_filename_is_reduced_to_basename(self):
        assert _upload_basename("../evil.zip") == "evil.zip"
        assert _upload_basename(r"..\evil.zip") == "evil.zip"


# ==================== Pydantic Model Validation Tests ====================

class TestAddDocumentRequest:
    """Test AddDocumentRequest Pydantic model validation."""

    def test_valid_minimal_request(self):
        """Minimal valid request should pass."""
        req = AddDocumentRequest(title="Test", content="Hello world")
        assert req.title == "Test"
        assert req.content == "Hello world"
        assert req.path == ""
        assert req.tags == []
        assert req.created_by == "api"

    def test_valid_full_request(self):
        """Full request with all fields should pass."""
        req = AddDocumentRequest(
            title="My Document",
            content="# Header\n\nContent here",
            path="docs/guides",
            tags=["guide", "tutorial"],
            created_by="admin",
        )
        assert req.title == "My Document"
        assert req.path == "docs/guides"
        assert len(req.tags) == 2

    def test_empty_title_rejected(self):
        """Empty title should fail validation."""
        with pytest.raises(Exception):
            AddDocumentRequest(title="", content="Some content")

    def test_empty_content_rejected(self):
        """Empty content should fail validation."""
        with pytest.raises(Exception):
            AddDocumentRequest(title="Test", content="")

    def test_title_too_long_rejected(self):
        """Title exceeding max length should fail."""
        with pytest.raises(Exception):
            AddDocumentRequest(title="A" * 501, content="Some content")

    def test_content_exceeds_max_size_rejected(self):
        """Content exceeding 10MB should fail."""
        with pytest.raises(Exception):
            AddDocumentRequest(title="Test", content="X" * (MAX_CONTENT_LENGTH + 1))

    def test_content_at_max_size_accepted(self):
        """Content at exactly 10MB should pass."""
        req = AddDocumentRequest(title="Test", content="X" * MAX_CONTENT_LENGTH)
        assert len(req.content) == MAX_CONTENT_LENGTH

    def test_tags_default_to_empty_list(self):
        """Tags should default to empty list when not provided."""
        req = AddDocumentRequest(title="Test", content="Hello")
        assert req.tags == []

    def test_path_too_long_rejected(self):
        """Path exceeding max length should fail."""
        with pytest.raises(Exception):
            AddDocumentRequest(title="Test", content="Hello", path="/" + "a" * 500)

    def test_empty_tags_list_accepted(self):
        """Empty tags list should pass."""
        req = AddDocumentRequest(title="Test", content="Hello", tags=[])
        assert req.tags == []


class TestUpdateDocumentRequest:
    """Test UpdateDocumentRequest Pydantic model validation."""

    def test_valid_update_request(self):
        """Valid update request should pass."""
        req = UpdateDocumentRequest(
            title="Updated", content="New content", path="new/path", tags=["updated"]
        )
        assert req.title == "Updated"
        assert req.updated_by == "api"

    def test_update_empty_title_rejected(self):
        """Empty title in update should fail."""
        with pytest.raises(Exception):
            UpdateDocumentRequest(title="", content="Content")

    def test_update_empty_content_rejected(self):
        """Empty content in update should fail."""
        with pytest.raises(Exception):
            UpdateDocumentRequest(title="Test", content="")

    def test_update_content_exceeds_max_rejected(self):
        """Content exceeding max size in update should fail."""
        with pytest.raises(Exception):
            UpdateDocumentRequest(title="Test", content="X" * (MAX_CONTENT_LENGTH + 1))


# ==================== Model Consistency Tests ====================

class TestModelConsistency:
    """Ensure API models and core models remain consistent."""

    def test_document_info_construction(self):
        """DocumentInfo should accept dict kwargs."""
        from models import DocumentInfo
        doc = DocumentInfo(
            doc_id="abc-123",
            title="Test Doc",
            path="docs/test",
            tags=["test"],
            chunk_count=5,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-05-24T00:00:00Z",
        )
        assert doc.doc_id == "abc-123"
        assert doc.chunk_count == 5

    def test_search_result_construction(self):
        """SearchResult should handle all fields."""
        from models import SearchResult
        sr = SearchResult(
            content="Found content",
            title="Document Title",
            path="some/path",
            source_path="documents/some/path/abc/source.md",
            doc_id="abc",
            chunk_index=2,
            total_chunks=10,
            score=0.85,
        )
        assert sr.score == 0.85
        assert 0.0 <= sr.score <= 1.0

    def test_admin_account_role_literal(self):
        """AdminAccount should only accept valid roles."""
        from models import AdminAccount
        # Valid roles
        AdminAccount(username="admin1", password_hash="hash1", role="super_admin", created_at="2026-01-01")
        AdminAccount(username="admin2", password_hash="hash2", role="admin", created_at="2026-01-01")


# ==================== Chunker integration test ====================

class TestChunkerWithSecurityContext:
    """Ensure chunker handles potentially malicious input gracefully."""

    def test_very_long_input(self):
        """Chunker should handle very long input without crashing."""
        from chunker import chunk_markdown
        long_md = "### Section\n\n" + "A" * 100000
        chunks = chunk_markdown(long_md, chunk_size=512, overlap=50)
        assert len(chunks) > 0

    def test_script_tags_handled_as_text(self):
        """Script tags in markdown should be treated as plain text."""
        from chunker import chunk_markdown
        md = "### Title\n\n<script>alert('xss')</script>\n\nNormal text here."
        chunks = chunk_markdown(md)
        assert len(chunks) > 0
        # The script tag should appear as text in some chunk
        combined = "".join(chunks)
        assert "script" in combined.lower()

    def test_null_bytes_handled(self):
        """Null bytes should not break chunker."""
        from chunker import chunk_markdown
        md = "### Title\n\nSome text.\x00More text."
        chunks = chunk_markdown(md)
        assert len(chunks) > 0

    def test_unicode_boundary(self):
        """Emoji and CJK characters should be handled correctly."""
        from chunker import chunk_markdown
        md = "### 🎮 游戏\n\n角色：吕布🎭、大乔👸\n\n测试データ"
        chunks = chunk_markdown(md)
        assert len(chunks) > 0
