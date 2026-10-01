"""/ask — 언제 답하지 않을지를 정하는 곳. md-rag-chatbot services/retrieval.py 에서 가져옴.

- 컷은 여기서 한다 (어댑터가 아니라). 통과한 건 전부 컨텍스트에 넣는다
- 컷 통과가 0개면 LLM 을 부르지 않는다. 최고점 하나라도 넣어주고 싶은 유혹을 참는다
- 본문 로드는 병렬 + 부분 실패 허용. "못 찾음"과 "못 읽음"은 원인이 달라서 문구를 나눈다
- SCORE_THRESHOLD 0.65 는 원래 볼트(실험②)에서 맞춘 값이다. 매 요청 top5 점수를 로그에 남기고 보고 조정한다
"""

import logging

from wrist_notes import gemini, i18n, notion, vectors
from wrist_notes.config import settings

log = logging.getLogger("wrist_notes")

TOP_K = 5


async def ask(question: str) -> str:
    vec = await gemini.embed(question, is_query=True)
    hits = await vectors.query(vec, TOP_K)
    log.info("[ask] q=%r scores=%s", question[:40], [f"{h.score:.3f} {h.title[:20]}" for h in hits])

    picked = [h for h in hits if h.score >= settings.score_threshold]
    if not picked:
        return i18n.t("no_match")

    bodies = await notion.gather_bodies([h.page_id for h in picked])
    notes = []
    for hit, body in zip(picked, bodies):
        if isinstance(body, BaseException):
            log.warning("[ask] 본문 로드 실패 %s: %r", hit.page_id, body)
            continue
        notes.append((hit.title, body))
    if not notes:
        return i18n.t("load_failed")

    answer, paid = await gemini.answer_from_notes(question, notes)
    # 유료로 넘어갔을 때만 붙인다. 워치가 소리 내 읽으니 '/' 같은 기호 없이
    return answer if paid is None else i18n.t("paid_answer", answer=answer, spent=i18n.money(paid))
