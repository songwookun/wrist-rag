import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise KeyError(f"env var {name} is empty / 환경변수 {name} 가 비어 있음")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    secret: str
    gemini_free_key: str
    gemini_paid_key: str
    gemini_model: str
    gemini_answer_model: str
    embedding_model: str
    embedding_dim: int
    notion_token: str
    notion_notes_db_id: str
    notion_usage_db_id: str
    pinecone_api_key: str
    pinecone_index: str
    save_mode: str
    self_url: str | None
    score_threshold: float
    price_in_usd_per_m: float
    price_out_usd_per_m: float
    price_search_usd: float
    usd_krw: float
    budget_krw: float
    note_cost_estimate_krw: float
    language: str
    timezone: str


def load() -> Settings:
    save_mode = _optional("SAVE_MODE", "async")
    if save_mode not in ("async", "sync"):
        raise ValueError(f"SAVE_MODE must be async or sync / SAVE_MODE 는 async 또는 sync: {save_mode}")
    return Settings(
        secret=_required("WRIST_SECRET"),
        gemini_free_key=_required("GEMINI_FREE_KEY"),
        # 정리는 검색이 필요해 유료로만 된다 → 필수
        gemini_paid_key=_required("GEMINI_PAID_KEY"),
        # 별칭이라 모델 은퇴에 안 깨진다. 대신 가리키는 모델이 바뀌면 단가도 바뀔 수 있다 → 로그의 model_version 확인
        gemini_model=_optional("GEMINI_MODEL", "gemini-flash-latest"),
        # 찾기 답변 모델. 노트 요약이라 lite 로 충분하고 무료 일일 한도가 넉넉하다. 503 지속 시 GEMINI_MODEL 로
        gemini_answer_model=_optional("GEMINI_ANSWER_MODEL", "gemini-flash-lite-latest"),
        embedding_model=_optional("EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dim=int(_optional("EMBEDDING_DIM", "768")),
        notion_token=_required("NOTION_TOKEN"),
        notion_notes_db_id=_required("NOTION_NOTES_DB_ID"),
        notion_usage_db_id=_required("NOTION_USAGE_DB_ID"),
        pinecone_api_key=_required("PINECONE_API_KEY"),
        pinecone_index=_optional("PINECONE_INDEX", "wrist-rag"),
        save_mode=save_mode,
        self_url=_optional("SELF_URL", "").rstrip("/") or None,
        score_threshold=float(_optional("SCORE_THRESHOLD", "0.65")),
        price_in_usd_per_m=float(_required("PRICE_IN_USD_PER_M")),
        price_out_usd_per_m=float(_required("PRICE_OUT_USD_PER_M")),
        price_search_usd=float(_optional("PRICE_SEARCH_USD", "0")),
        usd_krw=float(_optional("USD_KRW", "1400")),
        budget_krw=float(_optional("BUDGET_KRW", "4000")),
        # 실측 37~43원(VAT 포함). 검색 안 해서 재생성하면 두 배가 될 수 있어 넉넉히
        note_cost_estimate_krw=float(_optional("NOTE_COST_ESTIMATE_KRW", "50")),
        # 워치 응답·노션 이름·프롬프트 언어. wrist_notes/locales/<LANGUAGE>.json
        language=_optional("LANGUAGE", "ko"),
        # 노트 날짜와 '이번 달' 한도의 기준 시간대
        timezone=_optional("TIMEZONE", "Asia/Seoul"),
    )


settings = load()
