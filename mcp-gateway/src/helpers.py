"""Pure utility functions used across the knowledge base."""

import hashlib


def content_hash(content: str) -> str:
    """Calculate SHA256 hash of content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def content_size_kb(content: str) -> str:
    """Format content size as human-readable KB string."""
    size = len(content.encode("utf-8"))
    return f"{size / 1024:.1f}KB"
