"""Pinecone 색인. 점수는 거르지 않고 돌려준다 — 컷은 retrieval 이 한다.
SDK 가 동기라서 asyncio.to_thread 로 감싼다 (안 그러면 이벤트 루프 전체가 멈춘다)."""

import asyncio
from dataclasses import dataclass
from functools import lru_cache

from pinecone import Pinecone

from wrist_notes.config import settings


@lru_cache
def _index():
    return Pinecone(api_key=settings.pinecone_api_key).Index(name=settings.pinecone_index)


@dataclass(frozen=True)
class Hit:
    page_id: str
    title: str
    score: float


async def upsert(items: list[tuple[str, list[float], dict]]) -> None:
    """items: (노션 page_id, 벡터, metadata{title,url,date}). 같은 id 면 덮어쓴다."""
    vectors = [{"id": pid, "values": vec, "metadata": meta} for pid, vec, meta in items]
    await asyncio.to_thread(_index().upsert, vectors=vectors, show_progress=False)


async def query(vector: list[float], top_k: int = 5) -> list[Hit]:
    res = await asyncio.to_thread(
        _index().query, vector=vector, top_k=top_k, include_metadata=True
    )
    return [
        Hit(m.id, (m.metadata or {}).get("title", ""), m.score or 0.0)
        for m in res.matches or []
    ]
