"""Embeddings with BAAI/bge-small-en-v1.5 (architecture §5.5). Tests pass a fake embedder."""

import os
from collections.abc import Callable, Sequence

MODEL_NAME = "BAAI/bge-small-en-v1.5"
# bge models expect this prefix on search queries (not on passages). Used by retrieval in 4B.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

Embedder = Callable[[Sequence[str]], list[list[float]]]


class SentenceTransformerEmbedder:
    """Loads the model on first use. Normalized vectors, so cosine similarity = dot product."""

    def __init__(self, model_name: str = MODEL_NAME, batch_size: int = 32):
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            # Windows without Developer Mode can't create cache symlinks; copies work fine.
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]
