from unittest.mock import AsyncMock

import pytest

from main import _initialize_document_catalog


@pytest.mark.asyncio
async def test_startup_reconciles_catalog_before_keyword_index_refresh():
    events = []
    kb = AsyncMock()
    tools = AsyncMock()
    kb._doc_index_rebuild.side_effect = lambda: events.append("reconcile") or 2
    tools.refresh_keyword_index_safely.side_effect = (
        lambda reason: events.append(f"keyword:{reason}")
    )

    await _initialize_document_catalog(kb, tools)

    assert events == ["reconcile", "keyword:startup"]


@pytest.mark.asyncio
async def test_startup_refreshes_keyword_index_when_catalog_reconcile_fails():
    kb = AsyncMock()
    tools = AsyncMock()
    kb._doc_index_rebuild.side_effect = RuntimeError("chroma unavailable")

    await _initialize_document_catalog(kb, tools)

    tools.refresh_keyword_index_safely.assert_awaited_once_with("startup")
