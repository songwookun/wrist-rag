"""Gemini: 조사 노트 생성·파싱(유료) / 임베딩·노트 근거 답변(무료 우선). 키 선택은 budget 이 한다."""

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from google.genai import errors, types

from wrist_notes import budget, i18n
from wrist_notes.config import settings

log = logging.getLogger("wrist_notes")


@dataclass(frozen=True)
class Source:
    url: str     # 리다이렉트를 푼 원문 주소
    domain: str


@dataclass(frozen=True)
class Grounding:
    """검색을 실제로 했다는 근거. 비어 있으면 모델 지식만으로 쓴 것일 수 있다."""
    sources: list[Source] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Note:
    title: str
    keywords: list[str]
    summary: str
    body: str
    grounding: Grounding


# ── 생성 ────────────────────────────────────────────────

# 프롬프트 문구는 locales/<LANGUAGE>.json 의 prompts 에 있다. 필드 이름(TITLE 등)은 언어와 무관
_FIELD = re.compile(r"^[*# \t]*(TITLE|KEYWORDS|SUMMARY|BODY)[* \t]*:[* \t]*", re.M)


def parse_note(raw: str, request: str, grounding: Grounding) -> Note:
    marks = list(_FIELD.finditer(raw))
    parts = {
        m.group(1): raw[m.end() : nxt.start() if nxt else len(raw)].strip()
        for m, nxt in zip(marks, marks[1:] + [None])
    }
    if not parts.get("TITLE") or not parts.get("BODY"):
        return Note(request[:40], [], "", raw.strip(), grounding)
    return Note(
        title=parts["TITLE"].splitlines()[0].strip(),
        keywords=[k.strip() for k in parts.get("KEYWORDS", "").split(",") if k.strip()],
        summary=" ".join(parts.get("SUMMARY", "").split()),
        body=parts["BODY"],
        grounding=grounding,
    )


def _cited_chunks(resp: types.GenerateContentResponse) -> tuple[list[str], list[str]]:
    """(답변에 많이 인용된 순서의 리다이렉트 uri, 실행된 검색어)"""
    try:
        gm = resp.candidates[0].grounding_metadata
    except (AttributeError, IndexError, TypeError):
        gm = None
    if gm is None:
        return [], []
    chunks = gm.grounding_chunks or []
    cited = Counter(i for s in gm.grounding_supports or [] for i in s.grounding_chunk_indices or [])
    order = sorted(range(len(chunks)), key=lambda i: -cited[i])
    uris = [chunks[i].web.uri for i in order if chunks[i].web and chunks[i].web.uri]
    return uris, list(gm.web_search_queries or [])


async def _resolve(client: httpx.AsyncClient, uri: str) -> str:
    """그라운딩 uri 는 만료될 수 있는 구글 리다이렉트다. 302 의 Location 이 원문 주소"""
    try:
        r = await client.get(uri, follow_redirects=False)
        return r.headers.get("location") or uri
    except httpx.HTTPError:
        return uri


async def grounding_of(resp: types.GenerateContentResponse, limit: int = 5) -> Grounding:
    uris, queries = _cited_chunks(resp)
    async with httpx.AsyncClient(timeout=5) as c:
        urls = await asyncio.gather(*(_resolve(c, u) for u in uris))
    sources, seen = [], set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        sources.append(Source(url, urlparse(url).netloc.removeprefix("www.")))
    return Grounding(sources[:limit], queries)


async def _generate_grounded(prompt: str) -> tuple[types.GenerateContentResponse, float]:
    return await budget.paid(
        lambda c: c.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        ),
    )


async def generate_note(request: str, today: str) -> tuple[Note, float]:
    """반환: (노트, 이번 달 유료 누적 원)"""
    prompt = i18n.prompt("note").format(request=request, today=today)
    resp, paid = await _generate_grounded(prompt)
    grounding = await grounding_of(resp)
    if not grounding.queries:
        log.warning("[gemini] 검색 없이 답함 → 검색 강제 문구 붙여 1회 재생성")
        resp, paid = await _generate_grounded(prompt + i18n.prompt("must_search"))
        grounding = await grounding_of(resp)
    log.info("[gemini] model_version=%s 검색어=%s 출처=%d",
             resp.model_version, grounding.queries, len(grounding.sources))
    return parse_note(resp.text or "", request, grounding), paid


# ── 임베딩 ──────────────────────────────────────────────


def embed_text(title: str, keywords: list[str], summary: str) -> str:
    """/process 와 /reindex 가 공유한다. 둘이 다르면 재색인 후 점수 분포가 조용히 바뀐다."""
    return f"{title}\n{', '.join(keywords)}\n{summary}".strip()


async def embed_many(texts: list[str], *, is_query: bool = False) -> list[list[float]]:
    resp, _ = await budget.free_first(
        lambda c: c.aio.models.embed_content(
            model=settings.embedding_model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT",
                output_dimensionality=settings.embedding_dim,
            ),
        )
    )
    return [list(e.values) for e in resp.embeddings]


async def embed(text: str, *, is_query: bool = False) -> list[float]:
    return (await embed_many([text], is_query=is_query))[0]


# ── 노트 근거 답변 ───────────────────────────────────────

async def answer_from_notes(question: str, notes: list[tuple[str, str]]) -> tuple[str, float | None]:
    """반환: (답변, 유료로 넘어갔으면 이번 달 누적 원)"""
    context = "\n\n".join(f"--- [{title}] ---\n{body}" for title, body in notes)
    prompt = i18n.prompt("answer").format(notes=context, question=question, no_match=i18n.t("no_match"))

    async def ask(model: str):
        return await budget.free_first(
            lambda c: c.aio.models.generate_content(model=model, contents=prompt)
        )

    try:
        resp, paid = await ask(settings.gemini_answer_model)
    except errors.ServerError as e:
        # 과부하(503)가 재시도 후에도 계속되면 다른 모델로 한 번 더
        log.warning("[gemini] %s %s → 예비 모델 %s", settings.gemini_answer_model, e.code, settings.gemini_model)
        resp, paid = await ask(settings.gemini_model)
    return (resp.text or "").strip(), paid
