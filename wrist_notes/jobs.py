"""접수 → 처리. 큐 없이 노션 노트 DB 의 `상태`가 작업 목록이다.

/save 는 페이지만 만들고 자기 자신의 /process 를 부른 뒤 바로 답한다.
멈춘 작업은 다음 /save 때 sweep_stale 이 다시 부르거나(시도<2) 실패로 닫는다(시도≥2).
"""

import asyncio
import logging

import httpx

from wrist_notes import budget, gemini, i18n, notion, vectors
from wrist_notes.config import settings

log = logging.getLogger("wrist_notes")

MAX_ATTEMPTS = 2
STALE_MINUTES = 10  # maxDuration(60초)보다 충분히 길어야 도는 작업을 중복 처리하지 않는다
MAX_REFIRE = 3


def quota_reason(e: Exception) -> str | None:
    if isinstance(e, budget.RateLimited):
        return i18n.t("rate_limited")
    if isinstance(e, budget.BudgetExceeded):
        return i18n.t("budget_exceeded", usage=budget.usage_line(e.spent))
    return None


async def process(page_id: str) -> str:
    """실제 처리. 반환 문구는 SAVE_MODE=sync 일 때 워치에 그대로 간다."""
    job = await notion.read_job(page_id)
    if job.status != notion.PENDING:
        log.info("[process] %s 건너뜀 (상태 %s)", page_id, job.status)
        return i18n.t("already_done")

    attempt = job.attempts + 1
    await notion.set_attempts(page_id, attempt)
    log.info("[process] %s 시작 (시도 %d) %r", page_id, attempt, job.text[:40])

    try:
        note, paid = await gemini.generate_note(job.text, notion.today())
        url = await notion.fill_note(
            page_id,
            title=note.title,
            keywords=note.keywords,
            summary=note.summary,
            body=note.body,
            sources=[(s.domain, s.url) for s in note.grounding.sources],
            queries=note.grounding.queries,
            footer=i18n.t("paid_footer", usage=budget.usage_line(paid)),
            retry=attempt > 1,
        )
    except Exception as e:
        log.exception("[process] %s 생성·기록 실패 (시도 %d)", page_id, attempt)
        reason = quota_reason(e)
        if reason:
            await notion.fail(page_id, reason)
            return reason
        if attempt >= MAX_ATTEMPTS:
            await notion.fail(page_id, i18n.t("failed_n_times", n=MAX_ATTEMPTS, name=type(e).__name__))
            return i18n.t("failed", name=type(e).__name__)
        return i18n.t("retry_later")

    try:
        vec = await gemini.embed(gemini.embed_text(note.title, note.keywords, note.summary))
        await vectors.upsert([
            (page_id, vec, {"title": note.title, "url": url, "date": notion.today()})
        ])
    except Exception:
        log.exception("[process] %s 색인 실패", page_id)
        await notion.set_status(page_id, notion.INDEX_FAILED)
        return i18n.t("index_failed", title=note.title)

    await notion.set_status(page_id, notion.DONE)
    log.info("[process] %s 완료 %r", page_id, note.title)
    return i18n.t("saved", title=note.title, usage=budget.usage_line(paid))


async def fire(base_url: str, page_id: str) -> None:
    """자기 자신의 /process 를 부르되 응답은 안 기다린다. 요청이 전달되면 ReadTimeout 은 정상."""
    url = f"{settings.self_url or base_url.rstrip('/')}/process"
    # 연결·전송은 넉넉히, 응답 대기만 짧게. 읽기 단계에 들어갔다면 요청은 이미 다 보낸 것이다
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3, read=0.5)) as c:
            await c.post(url, json={"page_id": page_id}, headers={"X-Secret": settings.secret})
    except httpx.ReadTimeout:
        pass
    except Exception:
        # 전달 자체가 안 된 경우. 10분 뒤 sweep_stale 이 다시 부른다
        log.exception("[save] %s /process 호출 실패 (%s)", page_id, url)


async def sweep_stale(base_url: str) -> None:
    stale = await notion.find_stale(STALE_MINUTES)
    retry = [s for s in stale if s.attempts < MAX_ATTEMPTS][:MAX_REFIRE]
    dead = [s for s in stale if s.attempts >= MAX_ATTEMPTS]
    if retry or dead:
        log.info("[save] 멈춘 작업 재호출 %d / 실패 처리 %d", len(retry), len(dead))
    await asyncio.gather(
        *(fire(base_url, s.page_id) for s in retry),
        *(notion.fail(s.page_id, i18n.t("interrupted_twice")) for s in dead),
    )


async def reindex(cursor: str | None) -> tuple[int, str | None]:
    items, next_cursor = await notion.list_indexable(cursor)
    if items:
        vecs = await gemini.embed_many(
            [gemini.embed_text(i.title, i.keywords, i.summary) for i in items]
        )
        await vectors.upsert([
            (i.page_id, v, {"title": i.title, "url": i.url, "date": i.date})
            for i, v in zip(items, vecs, strict=True)
        ])
        await asyncio.gather(*(
            notion.set_status(i.page_id, notion.DONE)
            for i in items if i.status == notion.INDEX_FAILED
        ))
    return len(items), next_cursor
