import asyncio

import httpx
from langchain_core.embeddings import Embeddings

from rag.settings import Settings


class RouterEmbeddings(Embeddings):
    def __init__(self, settings: Settings):
        self.model = settings.embedding_model
        self.api_key = settings.routerai_api_key
        self.url = settings.routerai_base_url.rstrip("/") + "/embeddings"
        self.batch_size = settings.batch_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(await self._aembed_batch(texts[start : start + self.batch_size]))
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        vectors = await self._aembed_batch([text])
        return vectors[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        last_error = None
        for attempt in range(5):
            try:
                with httpx.Client(timeout=90) as client:
                    response = client.post(self.url, headers=self._headers(), json=self._payload(texts))
                    response.raise_for_status()
                    return _vectors(response.json())
            except httpx.HTTPError as error:
                last_error = error
                if attempt == 4:
                    break
        raise RuntimeError("Не удалось получить эмбеддинги") from last_error

    async def _aembed_batch(self, texts: list[str]) -> list[list[float]]:
        last_error = None
        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    response = await client.post(
                        self.url,
                        headers=self._headers(),
                        json=self._payload(texts),
                    )
                    response.raise_for_status()
                    return _vectors(response.json())
            except httpx.HTTPError as error:
                last_error = error
                if attempt == 4:
                    break
                await asyncio.sleep(min(2**attempt, 8))
        raise RuntimeError("Не удалось получить эмбеддинги") from last_error

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, texts: list[str]) -> dict:
        return {"model": self.model, "input": texts}


def build_embeddings(settings: Settings) -> RouterEmbeddings:
    return RouterEmbeddings(settings)


async def assert_embedding_dimension(embeddings: RouterEmbeddings, expected: int) -> None:
    vector = await embeddings.aembed_query("Иваново")
    actual = len(vector)
    if actual != expected:
        raise RuntimeError(f"Модель вернула вектор размерности {actual}, ожидалась {expected}")


def _vectors(payload: dict) -> list[list[float]]:
    rows = sorted(payload["data"], key=lambda item: item["index"])
    return [row["embedding"] for row in rows]
