"""Create the two Notion databases with the exact schema and write their IDs to .env.
노션 DB 두 개를 스키마 그대로 만들고 .env 에 ID 를 채운다.

Before / 준비:
  1. .env: NOTION_TOKEN (and LANGUAGE if not ko)
  2. In Notion, create an empty page → ··· → Connections → add your integration
     노션에 빈 페이지 하나 → ··· → 연결 → 내 Integration 추가
Run / 실행:  python scripts/setup_notion.py
"""

import json
import pathlib
import re
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
VERSION = "2026-03-11"


def env_get(name: str, default: str = "") -> str:
    m = re.search(rf"^{name}=(.*)$", ENV.read_text(), re.M) if ENV.exists() else None
    return (m.group(1).strip() if m else "") or default


def env_set(name: str, value: str) -> None:
    text = ENV.read_text()
    if re.search(rf"^{name}=", text, re.M):
        text = re.sub(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M)
    else:
        text += f"\n{name}={value}\n"
    ENV.write_text(text)


LANG = env_get("LANGUAGE", "ko")
LOCALE = json.loads((ROOT / "wrist_notes" / "locales" / f"{LANG}.json").read_text(encoding="utf-8"))
N = LOCALE["notion"]

http = httpx.Client(
    base_url="https://api.notion.com/v1",
    headers={"Authorization": f"Bearer {env_get('NOTION_TOKEN')}", "Notion-Version": VERSION},
    timeout=30,
)


def call(method: str, path: str, json: dict | None = None) -> dict:
    r = http.request(method, path, json=json)
    if r.is_error:
        sys.exit(f"{method} {path} → {r.status_code} {r.text}")
    return r.json()


def rich(text: str, bold: bool = False) -> list[dict]:
    return [{"type": "text", "text": {"content": text}, "annotations": {"bold": bold}}]


def find_parent() -> str:
    pages = call("POST", "/search", {"filter": {"property": "object", "value": "page"}})["results"]
    tops = [p for p in pages if p["parent"]["type"] == "workspace"] or pages
    if len(tops) != 1:
        sys.exit(f"Connect exactly one top-level page to the integration (found {len(tops)}). "
                 f"/ 연결된 최상위 페이지가 {len(tops)}개. 부모로 쓸 페이지 하나만 연결해줘")
    return tops[0]["id"]


def create_db(parent: str, title: str, emoji: str, props: dict) -> str:
    db = call("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent},
        "title": rich(title),
        "icon": {"type": "emoji", "emoji": emoji},
        "is_inline": True,
        "initial_data_source": {"properties": props},
    })
    return db["id"]


# 표에서 자주 보는 열을 앞에, 작업용(요청·시도)은 뒤에
NOTES = {
    N["title"]: {"type": "title", "title": {}},
    N["status"]: {"type": "select", "select": {"options": [
        {"name": N["pending"], "color": "yellow"},
        {"name": N["done"], "color": "green"},
        {"name": N["index_failed"], "color": "orange"},
        {"name": N["failed"], "color": "red"},
    ]}},
    N["keywords"]: {"type": "multi_select", "multi_select": {"options": []}},
    N["summary"]: {"type": "rich_text", "rich_text": {}},
    N["date"]: {"type": "date", "date": {}},
    N["sources"]: {"type": "rich_text", "rich_text": {}},
    N["request"]: {"type": "rich_text", "rich_text": {}},
    N["attempts"]: {"type": "number", "number": {"format": "number"}},
}

USAGE = {
    N["month"]: {"type": "title", "title": {}},
    N["cost"]: {"type": "number", "number": {"format": "number_with_commas"}},
    N["searches"]: {"type": "number", "number": {"format": "number_with_commas"}},
}

GUIDE = {
    "ko": {
        "page": "wrist-rag", "notes": "노트", "usage": "사용량",
        "callout": "워치에서 '정리' 단축어로 던져두면 몇 분 뒤 아래 노트 DB에 채워진다. "
                   "보기에서 필터 '상태 = 완료'를 걸어두면 결과만 보인다.",
        "heading": "상태 읽는 법",
        "rows": [
            ("pending", "10분 이내면 처리 중. 10분이 넘었으면 다음에 정리할 때 자동 재처리"),
            ("done", "정상. '찾기' 단축어로 검색됨"),
            ("index_failed", "노트는 완성, 검색에만 안 걸림 → /reindex 한 번"),
            ("failed", "두 번 실패 또는 한도 초과 → 페이지 본문 맨 아래 ❌ 줄에 이유"),
        ],
    },
    "en": {
        "page": "wrist-rag", "notes": "Notes", "usage": "Usage",
        "callout": "Send a request with the 'save' shortcut on your watch and it shows up in the Notes "
                   "database below in a few minutes. Filter the view by Status = Done to see results only.",
        "heading": "Reading the status",
        "rows": [
            ("pending", "Being processed if under 10 minutes. If older, it is retried on your next save"),
            ("done", "OK. Searchable with the 'ask' shortcut"),
            ("index_failed", "Note is complete but not searchable → call /reindex once"),
            ("failed", "Failed twice or hit the limit → reason on the ❌ line at the bottom of the page"),
        ],
    },
}


def write_guide(parent: str, g: dict) -> None:
    blocks = [
        {"type": "callout", "callout": {"icon": {"type": "emoji", "emoji": "⌚"}, "rich_text": rich(g["callout"])}},
        {"type": "heading_3", "heading_3": {"rich_text": rich(g["heading"])}},
    ]
    blocks += [
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich(f"{N[k]}  ", bold=True) + rich(desc)}}
        for k, desc in g["rows"]
    ]
    call("PATCH", f"/blocks/{parent}/children", {"children": blocks})


def main() -> None:
    if not env_get("NOTION_TOKEN"):
        sys.exit("NOTION_TOKEN is missing in .env / .env 에 NOTION_TOKEN 이 없음")
    if env_get("NOTION_NOTES_DB_ID"):
        sys.exit("NOTION_NOTES_DB_ID already set. Clear it in .env to recreate / 다시 만들려면 .env 에서 비우고 실행")
    g = GUIDE.get(LANG, GUIDE["en"])
    parent = find_parent()
    call("PATCH", f"/pages/{parent}", {
        "icon": {"type": "emoji", "emoji": "⌚"},
        "properties": {"title": {"title": rich(g["page"])}},
    })
    write_guide(parent, g)
    notes = create_db(parent, g["notes"], "📝", NOTES)
    usage = create_db(parent, g["usage"], "💰", USAGE)
    env_set("NOTION_NOTES_DB_ID", notes.replace("-", ""))
    env_set("NOTION_USAGE_DB_ID", usage.replace("-", ""))
    print(f"language={LANG}\nnotes DB  {notes}\nusage DB  {usage}\nwritten to .env")


if __name__ == "__main__":
    main()
