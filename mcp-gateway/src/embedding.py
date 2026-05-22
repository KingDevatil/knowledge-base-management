import httpx
from typing import List


class OllamaEmbedder:
    """Ollama Embedding 客户端"""

    def __init__(self, base_url: str, model: str = "bge-m3"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """批量生成文本的 Embedding 向量"""
        if not texts:
            return []

        embeddings = []
        for text in texts:
            response = await self.client.post(
                f"{self.base_url}/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": text,
                },
            )
            response.raise_for_status()
            data = response.json()
            embeddings.append(data["embedding"])

        return embeddings

    async def embed_single(self, text: str) -> List[float]:
        """单条文本 Embedding"""
        result = await self.embed([text])
        return result[0] if result else []

    async def health_check(self) -> bool:
        """检查 Ollama 服务是否可用"""
        try:
            response = await self.client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    async def close(self):
        await self.client.aclose()
