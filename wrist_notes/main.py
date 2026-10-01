import asyncio
import logging
import secrets
from collections.abc import Awaitable

import httpx
from fastapi import FastAPI, Request
from google.genai import errors
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from wrist_notes import budget, i18n, jobs, notion, retrieval
from wrist_notes.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("wrist_notes")
app = FastAPI()

OPEN_PATHS = {"/"}


@app.middleware("http")
async def check_secret(request: Request, call_next):
    given = request.headers.get("x-secret", "").encode()
    if request.url.path not in OPEN_PATHS and not secrets.compare_digest(
        given, settings.secret.encode()
    ):
        return JSONResponse({"ok": False, "message": i18n.t("auth_failed")})
    return await call_next(request)


# 단축어가 읽기 쉽게 응답은 항상 200 + {ok, message}
@app.exception_handler(RequestValidationError)
async def bad_request(request: Request, exc: RequestValidationError):
    log.warning("[%s] 요청 형식 오류 %s", request.url.path.strip("/"), exc.errors())
    return JSONResponse({"ok": False, "message": i18n.t("bad_request")})


def _reply(ok: bool, message: str) -> dict:
    return {"ok": ok, "message": message}


def fail_message(e: Exception) -> str:
    if reason := jobs.quota_reason(e):
        return reason
    if isinstance(e, errors.ServerError):
        return i18n.t("busy")
    if isinstance(e, httpx.HTTPStatusError):
        return i18n.t("http_failed", host=e.request.url.host, code=e.response.status_code)
    return i18n.t("failed", name=type(e).__name__)


async def _guard(tag: str, work: Awaitable[str]) -> dict:
    try:
        return _reply(True, await work)
    except Exception as e:
        log.exception("[%s] 실패", tag)
        return _reply(False, fail_message(e))


class TextIn(BaseModel):
    text: str


class PageIn(BaseModel):
    page_id: str


class CursorIn(BaseModel):
    cursor: str | None = None


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/save")
async def save(body: TextIn, request: Request):
    text = body.text.strip()
    if not text:
        return _reply(False, i18n.t("didnt_hear"))

    async def work() -> str:
        # 한도에 닿을 것 같으면 페이지도 만들지 않고 여기서 막는다 (BudgetExceeded → 문구)
        spent = await budget.check_headroom()
        page_id = await notion.create_pending(text)
        log.info("[save] 접수 %s %r", page_id, text[:40])
        if settings.save_mode == "sync":
            return await jobs.process(page_id)
        base = str(request.base_url)
        _, swept = await asyncio.gather(
            jobs.fire(base, page_id), jobs.sweep_stale(base), return_exceptions=True
        )
        if isinstance(swept, Exception):
            # 접수는 이미 됐다. 재처리 정리가 실패했다고 워치에 실패를 알릴 이유는 없다
            log.error("[save] 멈춘 작업 정리 실패: %r", swept)
        return i18n.t("accepted", usage=budget.usage_line(spent))

    return await _guard("save", work())


@app.post("/process")
async def process(body: PageIn):
    return await _guard("process", jobs.process(body.page_id))


@app.post("/ask")
async def ask(body: TextIn):
    text = body.text.strip()
    if not text:
        return _reply(False, i18n.t("didnt_hear"))
    return await _guard("ask", retrieval.ask(text))


@app.post("/reindex")
async def reindex(body: CursorIn):
    try:
        n, next_cursor = await jobs.reindex(body.cursor)
    except Exception as e:
        log.exception("[reindex] 실패")
        return _reply(False, fail_message(e))
    return {"ok": True, "indexed": n, "next_cursor": next_cursor}
