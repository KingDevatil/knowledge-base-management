"""Markdown HTML sanitization helpers."""
from __future__ import annotations

import bleach


ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union({
    "p", "pre", "code", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "hr", "br", "div", "span",
    "dl", "dt", "dd",
})

ALLOWED_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "code": ["class"],
    "pre": ["class"],
    "div": ["class", "id"],
    "span": ["class", "id"],
    "h1": ["id"],
    "h2": ["id"],
    "h3": ["id"],
    "h4": ["id"],
    "h5": ["id"],
    "h6": ["id"],
    "th": ["align"],
    "td": ["align"],
}

ALLOWED_PROTOCOLS = {"http", "https", "mailto", "tel"}


def sanitize_markdown_html(html: str) -> str:
    """Remove scriptable HTML while preserving common Markdown output."""
    return bleach.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
