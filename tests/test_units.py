import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from google.genai import errors

from wrist_notes import budget, gemini, i18n, notion
from wrist_notes.config import settings

# ── budget ────────────────────────────────────────────


class FakeUsage:
    def __init__(self, total=0.0):
        self.total = total
        self.added = []

    async def month_cost(self):
        return self.total

    async def add_month_cost(self, krw, searches=0):
        self.added.append(krw)
        self.total += krw
        return self.total


def _quota():
    return errors.ClientError(429, {"error": {"message": "quota"}})


def _call_log(fail_free: Exception | None):
    keys = []
    usage = SimpleNamespace(
        prompt_token_count=1_000_000, tool_use_prompt_token_count=0,
        candidates_token_count=500_000, thoughts_token_count=500_000,
    )

    async def call(client):
        keys.append(client._api_key)
        if fail_free and client._api_key == "free":
            raise fail_free
        return SimpleNamespace(usage_metadata=usage)

    return call, keys


@pytest.fixture
def usage(monkeypatch):
    u = FakeUsage()
    monkeypatch.setattr(notion, "month_cost", u.month_cost)
    monkeypatch.setattr(notion, "add_month_cost", u.add_month_cost)
    monkeypatch.setattr(budget, "_client", lambda key: SimpleNamespace(_api_key=key))
    return u


def _with(monkeypatch, **kw):
    monkeypatch.setattr(budget, "settings", dataclasses.replace(settings, **kw))


def _429(*details):
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": list(details)}})


DAILY = {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}
MINUTE = {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
          "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}
RETRY_3S = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []

    async def sleep(s):
        waits.append(s)

    monkeypatch.setattr(budget, "asyncio", SimpleNamespace(sleep=sleep))
    return waits


def test_quota_kind():
    assert budget.quota_kind(_429(DAILY, RETRY_3S)) == ("daily", 3.0)
    assert budget.quota_kind(_429(MINUTE, RETRY_3S)) == ("rate", 3.0)
    assert budget.quota_kind(_429()) == ("unknown", None)


def _seq(*outcomes):
    """키별 호출 기록 + 차례대로 예외/성공을 돌려주는 call"""
    keys = []

    async def call(client):
        keys.append(client._api_key)
        out = outcomes[len(keys) - 1]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(usage_metadata=None)

    return call, keys


def test_free_first_success_is_free(usage, no_sleep):
    call, keys = _seq("ok")
    _, paid = asyncio.run(budget.free_first(call))
    assert keys == ["free"] and paid is None and usage.added == []


def test_daily_exhausted_goes_paid(usage, no_sleep):
    call, keys = _seq(_429(DAILY), "ok")
    _, paid = asyncio.run(budget.free_first(call))
    assert keys == ["free", "paid"] and paid == 0 and no_sleep == []


def test_minute_limit_waits_and_retries_free_not_paid(usage, no_sleep):
    call, keys = _seq(_429(MINUTE, RETRY_3S), "ok")
    _, paid = asyncio.run(budget.free_first(call))
    assert keys == ["free", "free"] and paid is None and no_sleep == [3.0]


def test_minute_limit_twice_raises_without_paying(usage, no_sleep):
    call, keys = _seq(_429(MINUTE), _429(MINUTE))
    with pytest.raises(budget.RateLimited):
        asyncio.run(budget.free_first(call))
    assert keys == ["free", "free"]


def test_unknown_429_is_treated_as_temporary(usage, no_sleep):
    call, keys = _seq(_429(), _429())
    with pytest.raises(budget.RateLimited):
        asyncio.run(budget.free_first(call))
    assert "paid" not in keys and no_sleep == [2.0]


def test_minute_then_daily_goes_paid(usage, no_sleep):
    call, keys = _seq(_429(MINUTE), _429(DAILY), "ok")
    asyncio.run(budget.free_first(call))
    assert keys == ["free", "free", "paid"]


def test_wait_is_capped_at_10s(usage, no_sleep):
    long = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}
    call, _ = _seq(_429(long), "ok")
    asyncio.run(budget.free_first(call))
    assert no_sleep == [10.0]


def test_ask_fallback_respects_combined_budget(usage, no_sleep, monkeypatch):
    _with(monkeypatch, budget_krw=4000)
    usage.total = 3990  # 정리로 이미 거의 다 씀 + 찾기 예상 20원 > 4000
    call, keys = _seq(_429(DAILY), "ok")
    with pytest.raises(budget.BudgetExceeded):
        asyncio.run(budget.free_first(call))
    assert keys == ["free"]


def test_paid_goes_straight_to_paid_key_and_accumulates(usage, monkeypatch):
    _with(monkeypatch, usd_krw=1000)
    call, keys = _call_log(None)
    _, total = asyncio.run(budget.paid(call))
    # 단가 1$/M: 입력 1M + 출력(후보 0.5M + thinking 0.5M) = 2$ → 2000원
    assert keys == ["paid"]
    assert usage.added == [pytest.approx(2000)] and total == pytest.approx(2000)


def test_paid_blocked_when_next_note_would_exceed(usage, monkeypatch):
    _with(monkeypatch, budget_krw=4000, note_cost_estimate_krw=50)
    usage.total = 3951
    call, keys = _call_log(None)
    with pytest.raises(budget.BudgetExceeded) as exc:
        asyncio.run(budget.paid(call))
    assert keys == [] and exc.value.spent == 3951


def test_paid_allowed_just_under(usage, monkeypatch):
    _with(monkeypatch, budget_krw=4000, note_cost_estimate_krw=50)
    usage.total = 3950
    call, keys = _call_log(None)
    asyncio.run(budget.paid(call))
    assert keys == ["paid"]


def test_non_quota_error_is_raised_as_is(usage, no_sleep):
    call, keys = _seq(errors.ClientError(400, {"error": {"message": "bad"}}))
    with pytest.raises(errors.ClientError):
        asyncio.run(budget.free_first(call))
    assert keys == ["free"]


# ── 파싱·변환 ─────────────────────────────────────────


def test_parse_note():
    raw = "TITLE: GPU 비교\nKEYWORDS: GPU, 그래픽카드 , VRAM\nSUMMARY: 첫 문장.\n 둘째.\nBODY:\n## 개요\n- 항목"
    n = gemini.parse_note(raw, "요청", gemini.Grounding())
    assert n.title == "GPU 비교"
    assert n.keywords == ["GPU", "그래픽카드", "VRAM"]
    assert n.summary == "첫 문장. 둘째."
    assert n.body == "## 개요\n- 항목"


def test_parse_note_tolerates_bold_labels():
    n = gemini.parse_note("**TITLE:** 제목\n**BODY:**\n본문", "요청", gemini.Grounding())
    assert (n.title, n.body) == ("제목", "본문")


def test_parse_note_fallback():
    n = gemini.parse_note("형식 무시한 답", "아주 긴 요청" * 10, gemini.Grounding())
    assert n.title == ("아주 긴 요청" * 10)[:40]
    assert n.body == "형식 무시한 답"


def test_embed_text_is_shared_shape():
    assert gemini.embed_text("제목", ["a", "b"], "요약") == "제목\na, b\n요약"


def test_clean_keywords():
    assert notion.clean_keywords(["GPU", "gpu", "a,b", " ", *map(str, range(10))]) == [
        "GPU", "a b", "0", "1", "2", "3", "4", "5"
    ]


def test_body_blocks_split_and_sources():
    blocks = notion.body_blocks(
        "## 제목\n- 항목\n\n문단" + "가" * 2500, [("x.com", "https://x.com/a")], ["검색1"], "(유료)"
    )
    kinds = [b["type"] for b in blocks]
    assert kinds == ["heading_2", "bulleted_list_item", "paragraph", "heading_2",
                     "bulleted_list_item", "paragraph", "paragraph"]
    assert len(blocks[2]["paragraph"]["rich_text"]) == 2  # 2000자 조각
    assert blocks[4]["bulleted_list_item"]["rich_text"][0]["text"]["link"]["url"] == "https://x.com/a"
    assert blocks[5]["paragraph"]["rich_text"][0]["text"]["content"] == "검색어: 검색1"


def test_no_grounding_is_flagged_not_failed():
    blocks = notion.body_blocks("본문", [], [])
    assert blocks[-2]["heading_2"]["rich_text"][0]["text"]["content"] == i18n.t("sources_heading")
    assert blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"] == i18n.t("no_source")


def test_read_body_stops_at_sources(monkeypatch):
    blocks = notion.body_blocks("## 개요\n본문", [("x.com", "https://x.com")], ["q"], "(유료 사용)")
    for b in blocks:  # 노션 응답에는 plain_text 가 붙어 온다
        for r in b[b["type"]]["rich_text"]:
            r["plain_text"] = r["text"]["content"]

    async def children(pid):
        return blocks
    monkeypatch.setattr(notion, "_children", children)
    assert asyncio.run(notion.read_body("p")) == "개요\n본문"


def test_source_property_links_each_domain():
    rich = notion._source_property([("a.com", "https://a.com/1"), ("b.com", "https://b.com/2")])
    assert [r["text"]["content"] for r in rich] == ["a.com", ", ", "b.com"]
    assert rich[2]["text"]["link"]["url"] == "https://b.com/2"


def _resp(chunks, supports, queries):
    gm = SimpleNamespace(
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri=u, title="t")) for u in chunks],
        grounding_supports=[SimpleNamespace(grounding_chunk_indices=s) for s in supports],
        web_search_queries=queries,
    )
    return SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=gm)])


def test_grounding_orders_by_citation_resolves_and_dedupes(monkeypatch):
    real = {"r0": "https://www.a.com/x", "r1": "https://b.com/y", "r2": "https://www.a.com/x"}

    async def resolve(client, uri):
        return real[uri]

    monkeypatch.setattr(gemini, "_resolve", resolve)
    resp = _resp(["r0", "r1", "r2"], [[1], [1, 0], [1]], ["q1", "q2"])
    g = asyncio.run(gemini.grounding_of(resp))
    assert [(s.domain, s.url) for s in g.sources] == [("b.com", "https://b.com/y"), ("a.com", "https://www.a.com/x")]
    assert g.queries == ["q1", "q2"]


def test_grounding_absent():
    g = asyncio.run(gemini.grounding_of(SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=None)])))
    assert g == gemini.Grounding()


def test_search_count_and_cost(monkeypatch):
    monkeypatch.setattr(budget, "settings", dataclasses.replace(
        settings, price_in_usd_per_m=0, price_out_usd_per_m=0, price_search_usd=0.014, usd_krw=1000))
    resp = _resp([], [], ["a", "b", "c"])
    assert budget.search_count(resp) == 3
    assert budget.cost_krw(None, searches=3) == pytest.approx(42)


def test_5xx_retried_once_on_same_key(usage, no_sleep):
    call, keys = _seq(errors.ServerError(503, {"error": {"message": "overloaded"}}), "ok")
    _, paid = asyncio.run(budget.free_first(call))
    assert keys == ["free", "free"] and paid is None and no_sleep == [1]


def test_generate_note_retries_when_model_skipped_search(monkeypatch):
    prompts = []

    async def generate_content(model, contents, config):
        prompts.append(contents)
        searched = len(prompts) == 2  # 첫 호출은 검색을 건너뛴다
        resp = _resp(["r"] if searched else [], [[0]] if searched else [], ["검색됨"] if searched else [])
        return SimpleNamespace(text="TITLE: 제목\nBODY:\n본문", model_version="m", candidates=resp.candidates)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    async def fake_paid(call):
        return await call(client), 0.0

    async def resolve(c, uri):
        return "https://x.com/a"

    monkeypatch.setattr(budget, "paid", fake_paid)
    monkeypatch.setattr(gemini, "_resolve", resolve)
    note, _ = asyncio.run(gemini.generate_note("요청", "2026-10-01"))
    assert len(prompts) == 2 and i18n.prompt("must_search") in prompts[1]
    assert note.grounding.queries == ["검색됨"]
    assert note.grounding.sources == [gemini.Source("https://x.com/a", "x.com")]


def test_answer_uses_answer_model_then_main_model_on_503(monkeypatch):
    models = []

    async def fake_free_first(call):
        async def generate_content(model, contents):
            models.append(model)
            if model == settings.gemini_answer_model:
                raise errors.ServerError(503, {"error": {"message": "high demand"}})
            return SimpleNamespace(text=" 답 ")
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
        return await call(client), None

    monkeypatch.setattr(budget, "free_first", fake_free_first)
    text, paid = asyncio.run(gemini.answer_from_notes("q", [("t", "본문")]))
    assert (text, paid) == ("답", None)
    assert models == ["gemini-flash-lite-latest", "gemini-flash-latest"]



# ── 언어 파일 ────────────────────────────────────────────

import copy
import json


def _locale(lang):
    return json.loads((i18n.LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(i18n.LOCALES.glob("*.json")), ids=lambda p: p.name)
def test_every_shipped_locale_is_valid(path):
    i18n.validate(_locale(path.stem), _locale("ko"), path.stem)


def test_validate_catches_missing_key():
    data = _locale("en")
    del data["messages"]["no_match"]
    with pytest.raises(KeyError, match="no_match"):
        i18n.validate(data, _locale("ko"), "en")


def test_validate_catches_unknown_placeholder():
    data = _locale("en")
    data["messages"]["saved"] = "Saved {titel}"
    with pytest.raises(KeyError, match="titel"):
        i18n.validate(data, _locale("ko"), "en")


def test_validate_allows_dropping_a_placeholder():
    data = _locale("en")
    data["messages"]["accepted"] = "Got it"  # {usage} 를 안 보여주고 싶을 수 있다
    i18n.validate(data, _locale("ko"), "en")


def test_validate_requires_prompt_inputs():
    data = _locale("en")
    data["prompts"]["note"] = "Research this"  # {request} 가 빠지면 요청이 모델에 안 간다
    with pytest.raises(KeyError, match="request"):
        i18n.validate(data, _locale("ko"), "en")


def test_validate_requires_distinct_statuses():
    data = _locale("en")
    data["notion"]["done"] = data["notion"]["failed"]
    with pytest.raises(KeyError, match="statuses"):
        i18n.validate(data, _locale("ko"), "en")


def test_read_body_cuts_at_any_language_heading():
    assert {"출처", "Sources"} <= i18n.all_sources_headings()
