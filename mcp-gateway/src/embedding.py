import httpx
from dataclasses import dataclass
from time import monotonic
from typing import List, Protocol, Union

from logger import get_logger

logger = get_logger()


class EmbeddingError(Exception):
    """Embedding 生成异常，携带详细上下文供上层构造友好报错"""

    def __init__(
        self,
        message: str,
        expected_count: int = 0,
        actual_count: int = 0,
        batch_index: int | None = None,
        model: str = "",
    ):
        self.expected_count = expected_count
        self.actual_count = actual_count
        self.batch_index = batch_index
        self.model = model
        super().__init__(message)


class OllamaEmbedder:
    """Ollama Embedding 客户端 — 使用 /api/embed 批量端点"""

    # 单次批量请求的最大文本数（按 512 字符/chunk 估算，200 条约 100KB 请求体）
    DEFAULT_MAX_BATCH_SIZE = 200

    def __init__(
        self,
        base_url: str,
        model: str = "bge-m3",
        *,
        request_timeout_seconds: float = 120.0,
        health_timeout_seconds: float = 5.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 8,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{self.base_url}:{self.model}"
        self.health_timeout_seconds = max(0.1, float(health_timeout_seconds))
        self.max_batch_size = max(1, int(max_batch_size))
        max_connections = max(1, int(max_connections))
        max_keepalive_connections = max(
            0,
            min(int(max_keepalive_connections), max_connections),
        )
        self.client = httpx.AsyncClient(
            timeout=max(1.0, float(request_timeout_seconds)),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
        )

    async def embed(self, texts: Union[str, List[str]]) -> List[List[float]]:
        """生成文本 Embedding 向量。

        使用 Ollama 的 /api/embed 批量端点，一次请求提交所有文本。
        超过 MAX_BATCH_SIZE 时自动分批。

        Args:
            texts: 单条字符串或字符串列表

        Returns:
            embedding 向量列表，每个元素为 float 列表

        Raises:
            EmbeddingError: 当返回的向量数量与输入文本数量不匹配时
            httpx.HTTPError: 当 Ollama 服务返回非 2xx 响应时
        """
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []

        total_batches = (len(texts) + self.max_batch_size - 1) // self.max_batch_size
        all_embeddings = []

        for i in range(0, len(texts), self.max_batch_size):
            batch_index = i // self.max_batch_size
            batch = texts[i : i + self.max_batch_size]

            try:
                batch_embeddings = await self._embed_batch(batch, batch_index, total_batches)
            except EmbeddingError:
                raise
            except httpx.HTTPError as e:
                raise EmbeddingError(
                    f"Ollama 服务 ({self.base_url}) 返回错误: {e}。"
                    f"模型: {self.model}，批次: {batch_index + 1}/{total_batches}，"
                    f"本批文本数: {len(batch)}",
                    expected_count=len(batch),
                    actual_count=0,
                    batch_index=batch_index,
                    model=self.model,
                ) from e
            except Exception as e:
                raise EmbeddingError(
                    f"Embedding 请求失败: {e}。"
                    f"模型: {self.model}，批次: {batch_index + 1}/{total_batches}",
                    expected_count=len(batch),
                    actual_count=0,
                    batch_index=batch_index,
                    model=self.model,
                ) from e

            all_embeddings.extend(batch_embeddings)

        return all_embeddings

    async def _embed_batch(
        self, texts: List[str], batch_index: int = 0, total_batches: int = 1
    ) -> List[List[float]]:
        """单次批量 Embedding 调用"""
        response = await self.client.post(
            f"{self.base_url}/api/embed",
            json={
                "model": self.model,
                "input": texts,
            },
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings", [])

        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"Ollama 返回的向量数量不匹配: "
                f"期望 {len(texts)} 条，实际收到 {len(embeddings)} 条。"
                f"模型: {self.model}，批次: {batch_index + 1}/{total_batches}。"
                f"可能原因: 模型不支持批量 Embedding 或输入文本为空字符串。"
                f"请检查: 1) Ollama 版本是否 >= 0.1.26  2) 模型 {self.model} 是否已加载",
                expected_count=len(texts),
                actual_count=len(embeddings),
                batch_index=batch_index,
                model=self.model,
            )

        return embeddings

    async def embed_single(self, text: str) -> List[float]:
        """单条文本 Embedding"""
        try:
            result = await self.embed(text)
            return result[0] if result else []
        except EmbeddingError:
            raise

    async def health_check(self) -> bool:
        """检查 Ollama 服务是否可用"""
        try:
            response = await self.client.get(
                f"{self.base_url}/api/tags",
                timeout=self.health_timeout_seconds,
            )
            return response.status_code == 200
        except Exception:
            return False

    async def close(self):
        await self.client.aclose()


class EmbeddingProvider(Protocol):
    name: str

    async def embed(self, texts: Union[str, List[str]]) -> List[List[float]]:
        ...

    async def embed_single(self, text: str) -> List[float]:
        ...

    async def health_check(self) -> bool:
        ...

    async def close(self) -> None:
        ...


@dataclass
class ProviderCircuitState:
    failures: int = 0
    opened_at: float = 0.0
    health_checked_at: float = 0.0
    cached_health: bool | None = None


class ResilientEmbeddingProvider:
    """Embedding provider router with health cache and simple circuit breaker."""

    def __init__(
        self,
        providers: list[EmbeddingProvider],
        health_cache_ttl: int = 30,
        failure_threshold: int = 3,
        circuit_cooldown: int = 30,
    ):
        if not providers:
            raise ValueError("At least one embedding provider is required")
        self.providers = providers
        self.health_cache_ttl = max(0, health_cache_ttl)
        self.failure_threshold = max(1, failure_threshold)
        self.circuit_cooldown = max(1, circuit_cooldown)
        self._states = {provider.name: ProviderCircuitState() for provider in providers}

    async def embed(self, texts: Union[str, List[str]]) -> List[List[float]]:
        return await self._call("embed", texts)

    async def embed_single(self, text: str) -> List[float]:
        result = await self.embed(text)
        return result[0] if result else []

    async def health_check(self) -> bool:
        checks = [await self._cached_health_check(provider) for provider in self.providers]
        return any(checks)

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()

    def status(self) -> list[dict]:
        now = monotonic()
        result = []
        for provider in self.providers:
            state = self._states[provider.name]
            result.append({
                "name": provider.name,
                "failures": state.failures,
                "circuit_open": self._is_open(state, now),
                "cached_health": state.cached_health,
            })
        return result

    async def _call(self, method: str, *args):
        errors: list[str] = []
        for provider in self.providers:
            state = self._states[provider.name]
            if self._is_open(state):
                errors.append(f"{provider.name}: circuit open")
                continue

            try:
                value = await getattr(provider, method)(*args)
                self._record_success(state)
                return value
            except EmbeddingError as e:
                self._record_failure(state)
                errors.append(f"{provider.name}: {e}")
            except Exception as e:
                self._record_failure(state)
                errors.append(f"{provider.name}: {e}")

        raise EmbeddingError(
            "All embedding providers failed or are in cooldown. "
            + " | ".join(errors)
        )

    async def _cached_health_check(self, provider: EmbeddingProvider) -> bool:
        state = self._states[provider.name]
        now = monotonic()
        if (
            state.cached_health is not None
            and self.health_cache_ttl > 0
            and now - state.health_checked_at < self.health_cache_ttl
        ):
            return state.cached_health

        if self._is_open(state, now):
            state.cached_health = False
            state.health_checked_at = now
            return False

        try:
            ok = await provider.health_check()
        except Exception:
            ok = False
        state.cached_health = ok
        state.health_checked_at = now
        if ok:
            self._record_success(state)
        else:
            self._record_failure(state)
        return ok

    def _record_success(self, state: ProviderCircuitState) -> None:
        state.failures = 0
        state.opened_at = 0.0
        state.cached_health = True
        state.health_checked_at = monotonic()

    def _record_failure(self, state: ProviderCircuitState) -> None:
        state.failures += 1
        state.cached_health = False
        state.health_checked_at = monotonic()
        if state.failures >= self.failure_threshold:
            state.opened_at = monotonic()

    def _is_open(self, state: ProviderCircuitState, now: float | None = None) -> bool:
        if state.opened_at <= 0:
            return False
        now = now if now is not None else monotonic()
        if now - state.opened_at >= self.circuit_cooldown:
            state.opened_at = 0.0
            return False
        return True


def build_embedding_provider(
    primary_url: str,
    primary_model: str,
    fallback_specs: str = "",
    health_cache_ttl: int = 30,
    failure_threshold: int = 3,
    circuit_cooldown: int = 30,
    request_timeout_seconds: float = 120.0,
    health_timeout_seconds: float = 5.0,
    max_connections: int = 20,
    max_keepalive_connections: int = 8,
    max_batch_size: int = OllamaEmbedder.DEFAULT_MAX_BATCH_SIZE,
) -> ResilientEmbeddingProvider:
    provider_options = {
        "request_timeout_seconds": request_timeout_seconds,
        "health_timeout_seconds": health_timeout_seconds,
        "max_connections": max_connections,
        "max_keepalive_connections": max_keepalive_connections,
        "max_batch_size": max_batch_size,
    }
    providers: list[EmbeddingProvider] = [
        OllamaEmbedder(primary_url, primary_model, **provider_options)
    ]
    for spec in parse_embedding_fallbacks(fallback_specs):
        providers.append(OllamaEmbedder(spec["url"], spec["model"], **provider_options))
    return ResilientEmbeddingProvider(
        providers=providers,
        health_cache_ttl=health_cache_ttl,
        failure_threshold=failure_threshold,
        circuit_cooldown=circuit_cooldown,
    )


def parse_embedding_fallbacks(raw: str) -> list[dict[str, str]]:
    """Parse fallback specs: url|model,url|model. Model is optional."""
    fallbacks: list[dict[str, str]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "|" in item:
            url, model = item.split("|", 1)
        else:
            url, model = item, ""
        url = url.strip()
        model = model.strip() or "bge-m3"
        if url:
            fallbacks.append({"url": url, "model": model})
    return fallbacks
