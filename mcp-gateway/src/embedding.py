import httpx
from typing import List, Union

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
    MAX_BATCH_SIZE = 200

    def __init__(self, base_url: str, model: str = "bge-m3"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=120.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
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

        total_batches = (len(texts) + self.MAX_BATCH_SIZE - 1) // self.MAX_BATCH_SIZE
        all_embeddings = []

        for i in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch_index = i // self.MAX_BATCH_SIZE
            batch = texts[i : i + self.MAX_BATCH_SIZE]

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
            response = await self.client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    async def close(self):
        await self.client.aclose()
