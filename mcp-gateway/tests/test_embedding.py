import pytest

from src.embedding import (
    EmbeddingError,
    ResilientEmbeddingProvider,
    parse_embedding_fallbacks,
)


class FakeProvider:
    def __init__(self, name, *, fail=False, healthy=True):
        self.name = name
        self.fail = fail
        self.healthy = healthy
        self.embed_calls = 0
        self.health_calls = 0
        self.closed = False

    async def embed(self, texts):
        self.embed_calls += 1
        if self.fail:
            raise EmbeddingError(f"{self.name} failed")
        if isinstance(texts, str):
            texts = [texts]
        return [[float(self.embed_calls)] for _ in texts]

    async def embed_single(self, text):
        values = await self.embed(text)
        return values[0] if values else []

    async def health_check(self):
        self.health_calls += 1
        return self.healthy

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_resilient_provider_uses_fallback_when_primary_fails():
    primary = FakeProvider("primary", fail=True)
    fallback = FakeProvider("fallback")
    provider = ResilientEmbeddingProvider([primary, fallback], failure_threshold=3)

    result = await provider.embed(["a", "b"])

    assert result == [[1.0], [1.0]]
    assert primary.embed_calls == 1
    assert fallback.embed_calls == 1
    assert provider.status()[0]["failures"] == 1


@pytest.mark.asyncio
async def test_resilient_provider_opens_circuit_after_threshold():
    primary = FakeProvider("primary", fail=True)
    fallback = FakeProvider("fallback")
    provider = ResilientEmbeddingProvider(
        [primary, fallback],
        failure_threshold=1,
        circuit_cooldown=60,
    )

    await provider.embed("first")
    await provider.embed("second")

    assert primary.embed_calls == 1
    assert fallback.embed_calls == 2
    assert provider.status()[0]["circuit_open"] is True


@pytest.mark.asyncio
async def test_health_check_is_cached():
    primary = FakeProvider("primary", healthy=True)
    provider = ResilientEmbeddingProvider([primary], health_cache_ttl=60)

    assert await provider.health_check() is True
    assert await provider.health_check() is True

    assert primary.health_calls == 1


@pytest.mark.asyncio
async def test_close_closes_all_providers():
    primary = FakeProvider("primary")
    fallback = FakeProvider("fallback")
    provider = ResilientEmbeddingProvider([primary, fallback])

    await provider.close()

    assert primary.closed is True
    assert fallback.closed is True


def test_parse_embedding_fallbacks_supports_optional_model():
    result = parse_embedding_fallbacks("http://a:11434|model-a, http://b:11434")

    assert result == [
        {"url": "http://a:11434", "model": "model-a"},
        {"url": "http://b:11434", "model": "bge-m3"},
    ]
