"""Gemini 호출의 관문. 키 두 개의 역할이 고정돼 있다.

- 정리(검색 생성) → paid(): 유료 키 직행. 무료 등급은 검색 할당량이 0이라 출처가 남는 노트는 유료로만 된다
- 찾기·임베딩·재색인 → free_first(): 무료 키 먼저. **일일 할당량을 진짜 다 썼을 때만** 유료로 넘긴다.
  분당 제한(잠깐 몰림)은 기다렸다 무료로 한 번 더 하고, 그래도 막히면 유료로 넘기지 않는다
- 막는 기준은 이번 달 유료 누적 금액 하나, 정리·찾기 합산 (부가세 포함 환율 USD_KRW)
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, TypeVar

from google import genai
from google.genai import errors

from wrist_notes import i18n, notion
from wrist_notes.config import settings

log = logging.getLogger("wrist_notes")
T = TypeVar("T")


class RateLimited(Exception):
    """분당 제한 같은 일시적 429. 유료로 넘기지 않는다"""


class BudgetExceeded(Exception):
    def __init__(self, spent: float):
        super().__init__(f"{spent:.1f}원")
        self.spent = spent


@lru_cache
def _client(key: str) -> genai.Client:
    return genai.Client(api_key=key)


def _is_quota(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and e.code == 429


def search_count(result: Any) -> int:
    try:
        return len(result.candidates[0].grounding_metadata.web_search_queries or [])
    except (AttributeError, IndexError, TypeError):
        return 0


def cost_krw(usage: Any, *, searches: int) -> float:
    if usage is None:
        return searches * settings.price_search_usd * settings.usd_krw
    tokens_in = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
    # thinking 토큰도 출력 단가로 과금된다
    tokens_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    usd = (
        tokens_in / 1e6 * settings.price_in_usd_per_m
        + tokens_out / 1e6 * settings.price_out_usd_per_m
        + searches * settings.price_search_usd
    )
    return usd * settings.usd_krw


async def _once_more_on_5xx(call: Callable[[genai.Client], Awaitable[T]], key: str) -> T:
    """5xx(과부하 등)는 일시적이라 1초 쉬고 한 번만 다시 부른다"""
    try:
        return await call(_client(key))
    except errors.ServerError as e:
        log.warning("[budget] Gemini %s, 1초 뒤 재시도", e.code)
        await asyncio.sleep(1)
        return await call(_client(key))


# 찾기 1회를 유료로 할 때 예상 비용 = 정리 예상의 40%. 실측 약 10원 vs 정리 50원 설정.
# 비율로 둔 건 통화(원/달러)가 바뀌어도 그대로 맞게 하려고
ASK_RESERVE_RATIO = 0.4


async def check_headroom(reserve: float | None = None) -> float:
    """reserve 만큼 더 써도 한도 안인지. 넘을 것 같으면 BudgetExceeded. 반환: 이번 달 누적 원"""
    reserve = settings.note_cost_estimate_krw if reserve is None else reserve
    spent = await notion.month_cost()
    if spent + reserve > settings.budget_krw:
        raise BudgetExceeded(spent)
    return spent


def quota_kind(e: errors.APIError) -> tuple[str, float | None]:
    """429 본문에서 어떤 할당량인지 읽는다. ("daily" | "rate" | "unknown", 재시도 대기초)

    QuotaFailure.violations[].quotaId 예: GenerateRequestsPerDayPerProjectPerModel-FreeTier
    """
    body = e.details if isinstance(e.details, dict) else {}
    details = body.get("error", {}).get("details") or []
    ids, delay = [], None
    for d in details:
        kind = d.get("@type", "")
        if kind.endswith("QuotaFailure"):
            ids += [v.get("quotaId", "") for v in d.get("violations") or []]
        elif kind.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", d.get("retryDelay", ""))
            delay = float(m.group(1)) if m else None
    log.warning("[budget] 429 quotaId=%s retryDelay=%s", ids, delay)
    if any("PerDay" in i for i in ids):
        return "daily", delay
    if ids or delay is not None:
        return "rate", delay
    return "unknown", None


async def free_first(
    call: Callable[[genai.Client], Awaitable[T]], *, reserve: float | None = None
) -> tuple[T, float | None]:
    """반환: (결과, 유료로 넘어갔으면 이번 달 누적 원 / 무료면 None)"""
    try:
        return await _once_more_on_5xx(call, settings.gemini_free_key), None
    except errors.APIError as e:
        if not _is_quota(e):
            raise
        kind, delay = quota_kind(e)
    if kind != "daily":
        # 잠깐 몰린 것일 수 있다. 알려준 만큼(최대 10초) 쉬고 무료로 한 번 더
        wait = min(delay if delay is not None else 2.0, 10.0)
        log.warning("[budget] 무료 429(%s), %.0f초 뒤 무료로 재시도", kind, wait)
        await asyncio.sleep(wait)
        try:
            return await _once_more_on_5xx(call, settings.gemini_free_key), None
        except errors.APIError as e:
            if not _is_quota(e):
                raise
            kind, _ = quota_kind(e)
        if kind != "daily":
            raise RateLimited
    log.info("[budget] 무료 일일 할당량 소진 → 유료로 전환")
    if reserve is None:
        reserve = settings.note_cost_estimate_krw * ASK_RESERVE_RATIO
    return await paid(call, reserve=reserve)


async def paid(
    call: Callable[[genai.Client], Awaitable[T]], *, reserve: float | None = None
) -> tuple[T, float]:
    """반환: (결과, 이번 달 누적 원)"""
    await check_headroom(reserve)
    result = await _once_more_on_5xx(call, settings.gemini_paid_key)
    searches = search_count(result)
    krw = cost_krw(getattr(result, "usage_metadata", None), searches=searches)
    total = await notion.add_month_cost(krw, searches) if krw > 0 or searches else await notion.month_cost()
    log.info("[budget] 유료 %.1f원 검색 %d회, 이번 달 %.1f원", krw, searches, total)
    return result, total


def usage_line(total: float) -> str:
    return i18n.t("usage", total=i18n.money(total), budget=i18n.money(settings.budget_krw))
