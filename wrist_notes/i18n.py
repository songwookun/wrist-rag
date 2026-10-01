"""사용자에게 보이는 모든 글자는 locales/<LANGUAGE>.json 에 있다. 코드는 키로만 꺼내 쓴다.

- 사용자가 JSON 만 고쳐서 워치 응답·노션 이름·프롬프트를 바꿀 수 있게 하려는 구조
- 새 언어는 파일을 하나 추가하고 LANGUAGE 로 고른다 (예: ja.json + LANGUAGE=ja)
- 기동할 때 ko.json 을 기준으로 빠진 키·모르는 {자리}를 검사한다. 틀리면 어느 키인지 말하고 멈춘다
- notion 이름을 바꾸면 노션 DB 속성 이름도 같이 바꿔야 한다 (안 그러면 노션 400)
"""

import json
import string
from pathlib import Path

from wrist_notes.config import settings

LOCALES = Path(__file__).parent / "locales"
REFERENCE = "ko"

# 프롬프트에서 빠지면 안 되는 자리. 없으면 요청·노트가 모델에 안 들어간다
REQUIRED_PROMPT_FIELDS = {"note": {"request", "today"}, "answer": {"notes", "question"}}


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def _load(lang: str) -> dict:
    path = LOCALES / f"{lang}.json"
    if not path.exists():
        have = sorted(p.stem for p in LOCALES.glob("*.json"))
        raise KeyError(f"LANGUAGE={lang}: {path.name} not found. available: {have}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(data: dict, ref: dict, lang: str) -> None:
    for section in ("notion", "messages", "prompts"):
        missing = set(ref[section]) - set(data.get(section, {}))
        if missing:
            raise KeyError(f"{lang}.json [{section}] missing keys: {sorted(missing)}")
    for key, text in data["messages"].items():
        unknown = _fields(text) - _fields(ref["messages"].get(key, ""))
        if unknown:
            raise KeyError(f"{lang}.json messages.{key} unknown placeholders: {sorted(unknown)}")
    for key, need in REQUIRED_PROMPT_FIELDS.items():
        lost = need - _fields(data["prompts"][key])
        if lost:
            raise KeyError(f"{lang}.json prompts.{key} must contain placeholders: {sorted(lost)}")
    statuses = [data["notion"][k] for k in ("pending", "done", "index_failed", "failed")]
    if len(set(statuses)) != 4:
        raise KeyError(f"{lang}.json notion statuses must be 4 distinct values: {statuses}")


_ref = _load(REFERENCE)
_data = _load(settings.language)
validate(_data, _ref, settings.language)
_all_headings = {_load(p.stem)["messages"]["sources_heading"] for p in LOCALES.glob("*.json")}


def name(key: str) -> str:
    """노션 속성 이름·상태값"""
    return _data["notion"][key]


def t(key: str, **kw) -> str:
    return _data["messages"][key].format(**kw)


def prompt(key: str) -> str:
    return _data["prompts"][key]


def money(amount: float) -> str:
    """ko 는 원 단위 정수, 그 외는 달러 소수 둘째 자리 (USD_KRW=1 로 쓰는 전제)"""
    if settings.language == "ko":
        return f"{amount:,.0f}원"
    return f"${amount:,.2f}"


def all_sources_headings() -> set[str]:
    """read_body 가 끊는 지점. 언어를 바꿔도 예전 노트에서 끊기게 모든 언어의 제목을 본다"""
    return _all_headings
