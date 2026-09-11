"""向量化抽象（T41）：本地 TF-IDF（零依赖、演示即用）+ OpenAI 兼容 embedding 接口（真实模式）。

TF-IDF 向量为稀疏词典投影（规范化），余弦相似度可复算；配置 AI_TEST_EMBEDDING_API_KEY 后切真实模型。
"""
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Protocol


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class TfidfEmbedder:
    """本地 TF-IDF：idf 由入库文档统计，向量为词频×idf 的 L2 归一化。"""

    name = "tfidf"

    def __init__(self) -> None:
        self._doc_freq: Counter[str] = Counter()
        self._corpus_size = 0
        self._terms: dict[str, int] = {}  # 词 → 维度索引

    def fit(self, documents: list[list[str]]) -> None:
        """统计文档频率并固化词表（入库时调用一次）。"""
        seen = [set(d) for d in documents]
        for s in seen:
            for w in s:
                self._doc_freq[w] += 1
        self._corpus_size = max(len(documents), 1)
        self._terms = {w: i for i, w in enumerate(sorted(self._doc_freq))}

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 1)
        return math.log((self._corpus_size + 1) / (df + 1)) + 1.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        from .chunking import tokenize

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * len(self._terms)
            counts = Counter(tokenize(text))
            for term, c in counts.items():
                idx = self._terms.get(term)
                if idx is not None:
                    vec[idx] = c * self._idf(term)
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def dim(self) -> int:
        return len(self._terms)


class OpenAiEmbedder:
    """OpenAI 兼容 embedding（火山引擎/OpenAI）。配置 AI_TEST_EMBEDDING_API_KEY 启用。"""

    name = "openai"

    def __init__(self) -> None:
        import requests

        self._requests = requests
        self.base_url = os.environ.get("AI_TEST_EMBEDDING_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.api_key = os.environ.get("AI_TEST_EMBEDDING_API_KEY", "")
        self.model = os.environ.get("AI_TEST_EMBEDDING_MODEL", "doubao-embedding-large")

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._requests.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=30,
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]


def create_embedder() -> Embedder:
    if os.environ.get("AI_TEST_EMBEDDING_API_KEY"):
        return OpenAiEmbedder()
    return TfidfEmbedder()
