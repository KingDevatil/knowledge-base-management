"""Pure utility functions used across the knowledge base."""

import hashlib


def content_hash(content: str) -> str:
    """Calculate a newline-stable SHA256 hash for change detection.

    Browsers submit textarea newlines as CRLF, while stored Markdown normally
    uses LF. Treat those representations as the same document.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_size_kb(content: str) -> str:
    """Format content size as human-readable KB string."""
    size = len(content.encode("utf-8"))
    return f"{size / 1024:.1f}KB"
