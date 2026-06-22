"""Per-request MCP API key context.

MCP tool callbacks do not receive the FastAPI request object directly, so the
transport entry points set the authenticated API key info in a ContextVar before
delegating into the MCP server.
"""
from contextvars import ContextVar, Token

from models import APIKeyInfo


_MCP_API_KEY_INFO: ContextVar[APIKeyInfo | None] = ContextVar(
    "mcp_api_key_info",
    default=None,
)


def set_mcp_api_key_info(api_key_info: APIKeyInfo) -> Token:
    return _MCP_API_KEY_INFO.set(api_key_info)


def reset_mcp_api_key_info(token: Token) -> None:
    _MCP_API_KEY_INFO.reset(token)


def get_mcp_api_key_info() -> APIKeyInfo | None:
    return _MCP_API_KEY_INFO.get()
