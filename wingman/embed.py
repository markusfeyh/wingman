"""Local topic embeddings (no API key): fastembed + BAAI/bge-small-en-v1.5, 384 dims."""

import asyncio
import os
from functools import lru_cache

from . import graph


@lru_cache
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))


def _embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _model().embed(texts)]


async def embed(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed, texts) if texts else []


async def embed_missing_topics() -> int:
    ids = await graph.topics_missing_embeddings()
    for i in range(0, len(ids), 256):
        batch = ids[i:i + 256]
        await graph.set_topic_embeddings(dict(zip(batch, await embed(batch))))
    return len(ids)
