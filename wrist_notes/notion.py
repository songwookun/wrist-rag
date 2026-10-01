import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from wrist_notes import i18n
from wrist_notes.config import settings

log = logging.getLogger("wrist_notes")

NOTION_VERSION = "2026-03-11"
BASE = "https://api.notion.com/v1"
N = {k: i18n.name(k) for k in (
    "title", "status", "request", "attempts", "keywords", "summary", "date", "sources",
    "month", "cost", "searches",
)}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        headers={
            "Authorization": f"Bearer {settings.notion_token}",
            "Notion-Version": NOTION_VERSION,
        },
        timeout=15,
    )


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    async with _client() as c:
        r = await c.request(method, path, json=json)
    if r.is_error:
        log.error("[notion] %s %s → %s %s", method, path, r.status_code, r.text)
    r.raise_for_status()
    return r.json()


_ds_cache: dict[str, str] = {}


async def _data_source(database_id: str) -> str:
    if database_id not in _ds_cache:
        db = await _call("GET", f"/databases/{database_id}")
        sources = db["data_sources"]
        if len(sources) != 1:
            raise ValueError(f"Notion DB {database_id} has {len(sources)} data sources; expected 1")
        _ds_cache[database_id] = sources[0]["id"]
    return _ds_cache[database_id]


async def _query(database_id: str, body: dict) -> dict:
    ds = await _data_source(database_id)
    return await _call("POST", f"/data_sources/{ds}/query", body)


# 속성 이름·상태값은 locales/<LANGUAGE>.json 에서. select 는 오타를 새 옵션으로 조용히 만들어버리니
# 상태 문자열은 이 상수로만 쓴다.
PENDING, DONE, INDEX_FAILED, FAILED = (i18n.name(k) for k in ("pending", "done", "index_failed", "failed"))


def _text(value: str) -> list[dict]:
    return [{"text": {"content": value[i : i + 2000]}} for i in range(0, len(value), 2000)]


def _plain(rich: list[dict]) -> str:
    return "".join(t["plain_text"] for t in rich)


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def today() -> str:
    return _now().date().isoformat()


# ── 노트: 접수 ────────────────────────────────────────────


async def create_pending(text: str) -> str:
    page = await _call("POST", "/pages", {
        "parent": {
            "type": "data_source_id",
            "data_source_id": await _data_source(settings.notion_notes_db_id),
        },
        "properties": {
            N["title"]: {"title": _text(f"⏳ {text[:30]}")},
            N["status"]: {"select": {"name": PENDING}},
            N["request"]: {"rich_text": _text(text)},
            N["attempts"]: {"number": 0},
        },
    })
    return page["id"]


@dataclass(frozen=True)
class Job:
    text: str
    attempts: int
    status: str | None


async def read_job(page_id: str) -> Job:
    p = (await _call("GET", f"/pages/{page_id}"))["properties"]
    return Job(
        text=_plain(p[N["request"]]["rich_text"]),
        attempts=p[N["attempts"]]["number"] or 0,
        status=(p[N["status"]]["select"] or {}).get("name"),
    )


async def _patch(page_id: str, properties: dict) -> dict:
    return await _call("PATCH", f"/pages/{page_id}", {"properties": properties})


async def set_attempts(page_id: str, n: int) -> None:
    await _patch(page_id, {N["attempts"]: {"number": n}})


async def set_status(page_id: str, status: str) -> None:
    await _patch(page_id, {N["status"]: {"select": {"name": status}}})


# ── 노트: 본문 블록 ───────────────────────────────────────


def _block(kind: str, text: str, link: str | None = None) -> dict:
    rich = _text(text)
    if link:
        rich = [{"text": {"content": text[:2000], "link": {"url": link}}}]
    return {"object": "block", "type": kind, kind: {"rich_text": rich}}




def _short(url: str, limit: int = 90) -> str:
    s = url.split("://", 1)[-1]
    return s if len(s) <= limit else s[: limit - 1] + "…"


def body_blocks(
    body: str,
    sources: list[tuple[str, str]],
    queries: list[str],
    footer: str | None = None,
) -> list[dict]:
    """sources: (도메인, 원문 주소). 출처 블록은 항상 '출처' 제목 아래 — read_body 가 여기서 끊는다"""
    blocks = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith(("- ", "* ")):
            blocks.append(_block("bulleted_list_item", line[2:]))
        else:
            blocks.append(_block("paragraph", line))
    blocks.append(_block("heading_2", i18n.t("sources_heading")))
    if sources:
        blocks.extend(_block("bulleted_list_item", _short(url), link=url) for _, url in sources)
    else:
        blocks.append(_block("paragraph", i18n.t("no_source")))
    if queries:
        blocks.append(_block("paragraph", i18n.t("queries", queries=" · ".join(queries))))
    if footer:
        blocks.append(_block("paragraph", footer))
    return blocks


def _source_property(sources: list[tuple[str, str]]) -> list[dict]:
    """표 보기에서 바로 보이게 도메인마다 링크를 건다"""
    rich = []
    for i, (domain, url) in enumerate(sources):
        if i:
            rich.append({"text": {"content": ", "}})
        rich.append({"text": {"content": domain[:200], "link": {"url": url}}})
    return rich


async def _append(page_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), 100):
        await _call("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i : i + 100]})


async def _clear_children(page_id: str) -> None:
    # 재시도 때 앞선 시도가 반쯤 써둔 본문이 중복되지 않게 비운다
    ids = [b["id"] for b in await _children(page_id)]
    for block_id in ids:
        await _call("DELETE", f"/blocks/{block_id}")


async def _children(page_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        q = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
        res = await _call("GET", f"/blocks/{page_id}/children{q}")
        out.extend(res["results"])
        if not res.get("has_more"):
            return out
        cursor = res["next_cursor"]


def clean_keywords(keywords: list[str]) -> list[str]:
    seen, out = set(), []
    for k in keywords:
        k = k.replace(",", " ").strip()[:100]
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out[:8]


async def fill_note(
    page_id: str,
    *,
    title: str,
    keywords: list[str],
    summary: str,
    body: str,
    sources: list[tuple[str, str]],
    queries: list[str],
    footer: str | None,
    retry: bool,
) -> str:
    """본문을 먼저, 속성을 나중에 쓴다. 속성까지 써져야 '채워진' 것이다. 반환: 페이지 URL"""
    if retry:
        await _clear_children(page_id)
    await _append(page_id, body_blocks(body, sources, queries, footer))
    page = await _patch(page_id, {
        N["title"]: {"title": _text(title[:2000])},
        N["keywords"]: {"multi_select": [{"name": k} for k in clean_keywords(keywords)]},
        N["summary"]: {"rich_text": _text(summary[:2000])},
        N["date"]: {"date": {"start": today()}},
        N["sources"]: {"rich_text": _source_property(sources)},
    })
    return page["url"]


async def fail(page_id: str, reason: str) -> None:
    await _append(page_id, [_block("paragraph", f"❌ {reason}")])
    await set_status(page_id, FAILED)


async def append_line(page_id: str, text: str) -> None:
    await _append(page_id, [_block("paragraph", text)])


def _block_text(block: dict) -> str:
    data = block.get(block["type"], {})
    return _plain(data.get("rich_text", [])) if isinstance(data, dict) else ""


async def read_body(page_id: str) -> str:
    """찾기 답변의 근거. 출처 목록·검색어·유료 표시는 근거가 아니라서 '출처' 제목에서 끊는다"""
    lines = []
    for block in await _children(page_id):
        text = _block_text(block)
        if block["type"] == "heading_2" and text in i18n.all_sources_headings():
            break
        if text:
            lines.append(text)
    return "\n".join(lines)


# ── 노트: 조회 ────────────────────────────────────────────


@dataclass(frozen=True)
class Stale:
    page_id: str
    attempts: int


async def find_stale(minutes: int = 10) -> list[Stale]:
    before = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    res = await _query(settings.notion_notes_db_id, {
        "filter": {"and": [
            {"property": N["status"], "select": {"equals": PENDING}},
            {"timestamp": "created_time", "created_time": {"before": before}},
        ]},
        "page_size": 20,
    })
    return [Stale(p["id"], p["properties"][N["attempts"]]["number"] or 0) for p in res["results"]]


@dataclass(frozen=True)
class Indexable:
    page_id: str
    url: str
    title: str
    keywords: list[str]
    summary: str
    date: str
    status: str


async def list_indexable(cursor: str | None, size: int = 50) -> tuple[list[Indexable], str | None]:
    body = {
        "filter": {"or": [
            {"property": N["status"], "select": {"equals": DONE}},
            {"property": N["status"], "select": {"equals": INDEX_FAILED}},
        ]},
        "page_size": size,
    }
    if cursor:
        body["start_cursor"] = cursor
    res = await _query(settings.notion_notes_db_id, body)
    items = []
    for p in res["results"]:
        pr = p["properties"]
        items.append(Indexable(
            page_id=p["id"],
            url=p["url"],
            title=_plain(pr[N["title"]]["title"]),
            keywords=[o["name"] for o in pr[N["keywords"]]["multi_select"]],
            summary=_plain(pr[N["summary"]]["rich_text"]),
            date=(pr[N["date"]]["date"] or {}).get("start", ""),
            status=pr[N["status"]]["select"]["name"],
        ))
    return items, res.get("next_cursor") if res.get("has_more") else None


# ── 사용량 DB ────────────────────────────────────────────


def this_month() -> str:
    return _now().strftime("%Y-%m")


async def _usage_page(month: str) -> dict | None:
    res = await _query(settings.notion_usage_db_id, {
        "filter": {"property": N["month"], "title": {"equals": month}},
        "page_size": 1,
    })
    return res["results"][0] if res["results"] else None


async def month_cost() -> float:
    page = await _usage_page(this_month())
    return (page["properties"][N["cost"]]["number"] or 0) if page else 0.0


async def add_month_cost(krw: float, searches: int = 0) -> float:
    """이번 달 누적에 더하고 새 누적을 돌려준다. 1인용이라 동시 쓰기 경합은 무시한다."""
    month = this_month()
    page = await _usage_page(month)
    if page is None:
        await _call("POST", "/pages", {
            "parent": {
                "type": "data_source_id",
                "data_source_id": await _data_source(settings.notion_usage_db_id),
            },
            "properties": {
                N["month"]: {"title": _text(month)},
                N["cost"]: {"number": round(krw, 1)},
                N["searches"]: {"number": searches},
            },
        })
        return krw
    props = page["properties"]
    total = (props[N["cost"]]["number"] or 0) + krw
    await _patch(page["id"], {
        N["cost"]: {"number": round(total, 1)},
        N["searches"]: {"number": (props[N["searches"]]["number"] or 0) + searches},
    })
    return total


async def gather_bodies(page_ids: list[str]) -> list[str | BaseException]:
    return await asyncio.gather(*(read_body(p) for p in page_ids), return_exceptions=True)
