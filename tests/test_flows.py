"""외부 API(노션·Gemini·Pinecone)는 전부 가짜로 바꿔서 흐름만 검증한다."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from wrist_notes import budget, gemini, i18n, jobs, notion, vectors
from wrist_notes.config import settings
from wrist_notes.main import app

H = {"X-Secret": "test-secret"}
NOTE = gemini.Note(
    "GPU 비교", ["GPU", "그래픽카드"], "요약입니다.", "## 개요\n- 항목",
    gemini.Grounding([gemini.Source("https://a.com/x", "a.com")], ["gpu 비교"]),
)


class FakeNotion:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.failed: dict[str, str] = {}
        self.fill_calls: list[dict] = []
        self.stale: list[notion.Stale] = []
        self.bodies: dict[str, str | Exception] = {}
        self.spent = 0.0

    async def month_cost(self):
        return self.spent

    async def create_pending(self, text):
        pid = f"p{len(self.pages) + 1}"
        self.pages[pid] = {"text": text, "attempts": 0, "status": notion.PENDING}
        return pid

    async def read_job(self, pid):
        p = self.pages[pid]
        return notion.Job(p["text"], p["attempts"], p["status"])

    async def set_attempts(self, pid, n):
        self.pages[pid]["attempts"] = n

    async def set_status(self, pid, status):
        self.pages[pid]["status"] = status

    async def fill_note(self, pid, **kw):
        self.fill_calls.append({"pid": pid, **kw})
        return f"https://notion.so/{pid}"

    async def fail(self, pid, reason):
        self.failed[pid] = reason
        self.pages.setdefault(pid, {})["status"] = notion.FAILED

    async def find_stale(self, minutes):
        return self.stale

    async def gather_bodies(self, pids):
        return [self.bodies[p] for p in pids]


@pytest.fixture
def fake(monkeypatch):
    fn = FakeNotion()
    for name in ("create_pending", "read_job", "set_attempts", "set_status",
                 "fill_note", "fail", "find_stale", "gather_bodies", "month_cost"):
        monkeypatch.setattr(notion, name, getattr(fn, name))

    fn.upserts = []
    fn.fired = []
    fn.gen_error = None
    fn.embed_error = None
    fn.answer_calls = []
    fn.hits = []

    async def generate_note(text, today):
        if fn.gen_error:
            raise fn.gen_error
        return NOTE, 37.4

    async def embed(text, *, is_query=False):
        if fn.embed_error:
            raise fn.embed_error
        return [0.1, 0.2]

    async def upsert(items):
        fn.upserts.extend(items)

    async def query(vec, top_k=5):
        return fn.hits

    async def answer(q, notes):
        fn.answer_calls.append(notes)
        return "답이야", None

    async def fire(base, pid):
        fn.fired.append(pid)

    monkeypatch.setattr(gemini, "generate_note", generate_note)
    monkeypatch.setattr(gemini, "embed", embed)
    monkeypatch.setattr(gemini, "answer_from_notes", answer)
    monkeypatch.setattr(vectors, "upsert", upsert)
    monkeypatch.setattr(vectors, "query", query)
    monkeypatch.setattr(jobs, "fire", fire)
    return fn


client = TestClient(app)


def post(path, **json):
    r = client.post(path, json=json, headers=H)
    assert r.status_code == 200
    return r.json()


# ── /save ─────────────────────────────────────────────


def test_save_creates_pending_fires_and_answers_now(fake):
    r = post("/save", text="GPU 뭐 사야 돼")
    assert r == {"ok": True, "message": "접수했어, 몇 분 뒤 노션 봐줘 (이번 달 0원 / 4,000원)"}
    assert fake.pages["p1"]["status"] == notion.PENDING
    assert fake.fired == ["p1"]
    assert fake.fill_calls == []  # 처리는 /process 몫


def test_save_refires_only_retryable_stale_and_closes_dead(fake):
    fake.stale = [notion.Stale(f"s{i}", 0) for i in range(5)] + [notion.Stale("dead", 2)]
    post("/save", text="x")
    assert fake.fired == ["p1", "s0", "s1", "s2"]  # 새 것 + 최대 3개
    assert fake.failed == {"dead": "처리가 두 번 끊겼어"}


def test_save_fails_when_page_cannot_be_created(fake, monkeypatch):
    async def boom(text):
        raise RuntimeError

    monkeypatch.setattr(notion, "create_pending", boom)
    assert post("/save", text="x") == {"ok": False, "message": "실패했어. RuntimeError"}


def test_save_blocked_before_page_when_budget_would_be_exceeded(fake):
    fake.spent = 3960  # + 예상 50원 > 4,000원
    r = post("/save", text="x")
    assert r == {"ok": False, "message": "이번 달 유료 한도에 닿아서 멈췄어. 이번 달 3,960원 / 4,000원"}
    assert fake.pages == {} and fake.fired == []


def test_save_reports_month_usage(fake):
    fake.spent = 1234.5
    assert post("/save", text="x")["message"].endswith("(이번 달 1,234원 / 4,000원)")


def test_save_empty_dictation(fake):
    assert post("/save", text="  ")["ok"] is False


def test_bad_body_still_200(fake):
    assert post("/save", nope=1)["ok"] is False


def test_save_sync_mode_processes_inline(fake, monkeypatch):
    monkeypatch.setattr(jobs, "settings", dataclasses.replace(settings, save_mode="sync"))
    monkeypatch.setattr("wrist_notes.main.settings", dataclasses.replace(settings, save_mode="sync"))
    r = post("/save", text="x")
    assert r["message"] == "저장했어. GPU 비교 (이번 달 37원 / 4,000원)"
    assert fake.fired == []
    assert fake.pages["p1"]["status"] == notion.DONE


# ── /process ──────────────────────────────────────────


def test_process_success(fake):
    pid = "p1"
    fake.pages[pid] = {"text": "요청", "attempts": 0, "status": notion.PENDING}
    assert post("/process", page_id=pid)["ok"] is True
    assert fake.pages[pid] == {"text": "요청", "attempts": 1, "status": notion.DONE}
    assert fake.upserts[0][0] == pid
    assert fake.upserts[0][2]["url"] == "https://notion.so/p1"
    assert fake.fill_calls[0]["retry"] is False
    assert fake.fill_calls[0]["sources"] == [("a.com", "https://a.com/x")]
    assert fake.fill_calls[0]["queries"] == ["gpu 비교"]


def test_process_first_failure_stays_pending(fake):
    fake.pages["p1"] = {"text": "요청", "attempts": 0, "status": notion.PENDING}
    fake.gen_error = RuntimeError("x")
    post("/process", page_id="p1")
    assert fake.pages["p1"]["status"] == notion.PENDING
    assert fake.pages["p1"]["attempts"] == 1
    assert fake.failed == {}


def test_process_second_failure_is_final(fake):
    fake.pages["p1"] = {"text": "요청", "attempts": 1, "status": notion.PENDING}
    fake.gen_error = RuntimeError("x")
    post("/process", page_id="p1")
    assert fake.pages["p1"]["status"] == notion.FAILED
    assert "RuntimeError" in fake.failed["p1"]


def test_process_retry_clears_previous_body(fake):
    fake.pages["p1"] = {"text": "요청", "attempts": 1, "status": notion.PENDING}
    post("/process", page_id="p1")
    assert fake.fill_calls[0]["retry"] is True


def test_process_quota_fails_immediately(fake):
    fake.pages["p1"] = {"text": "요청", "attempts": 0, "status": notion.PENDING}
    fake.gen_error = budget.BudgetExceeded(4000)
    post("/process", page_id="p1")
    assert fake.failed["p1"] == "이번 달 유료 한도에 닿아서 멈췄어. 이번 달 4,000원 / 4,000원"


def test_process_index_failure(fake):
    fake.pages["p1"] = {"text": "요청", "attempts": 0, "status": notion.PENDING}
    fake.embed_error = RuntimeError("pinecone")
    post("/process", page_id="p1")
    assert fake.pages["p1"]["status"] == notion.INDEX_FAILED


@pytest.mark.parametrize("status", [notion.DONE, notion.FAILED, notion.INDEX_FAILED, None])
def test_process_skips_non_pending(fake, status):
    fake.pages["p1"] = {"text": "요청", "attempts": 0, "status": status}
    post("/process", page_id="p1")
    assert fake.pages["p1"]["attempts"] == 0
    assert fake.fill_calls == []


# ── /ask ──────────────────────────────────────────────


def test_ask_below_threshold_never_calls_llm(fake):
    fake.hits = [vectors.Hit("p1", "t", 0.64)]
    assert post("/ask", text="뭐였지")["message"] == i18n.t("no_match")
    assert fake.answer_calls == []


def test_ask_uses_only_hits_above_threshold_and_tolerates_partial_load(fake):
    fake.hits = [vectors.Hit("a", "A", 0.9), vectors.Hit("b", "B", 0.7), vectors.Hit("c", "C", 0.5)]
    fake.bodies = {"a": "본문A", "b": RuntimeError("notion")}
    assert post("/ask", text="q")["message"] == "답이야"
    assert fake.answer_calls == [[("A", "본문A")]]


def test_ask_all_loads_failed(fake):
    fake.hits = [vectors.Hit("a", "A", 0.9)]
    fake.bodies = {"a": RuntimeError()}
    assert post("/ask", text="q")["message"] == i18n.t("load_failed")
    assert fake.answer_calls == []


def test_ask_rate_limited_never_goes_paid(fake, monkeypatch):
    async def limited(text, *, is_query=False):
        raise budget.RateLimited

    monkeypatch.setattr(gemini, "embed", limited)
    assert post("/ask", text="q") == {"ok": False, "message": "잠깐 요청이 몰렸어. 조금 뒤에 다시 해줘"}


def test_ask_paid_fallback_announces_usage(fake, monkeypatch):
    fake.hits = [vectors.Hit("a", "A", 0.9)]
    fake.bodies = {"a": "본문"}

    async def answer(q, notes):
        return "답이야", 1234.5

    monkeypatch.setattr(gemini, "answer_from_notes", answer)
    assert post("/ask", text="q")["message"] == "답이야 무료 한도가 다 돼서 유료로 답했어. 이번 달 1,234원 썼어."
