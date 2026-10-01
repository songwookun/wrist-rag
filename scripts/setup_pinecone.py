"""Create the Pinecone index (cosine, EMBEDDING_DIM) if it doesn't exist.
Pinecone 인덱스를 만든다 (cosine, EMBEDDING_DIM). 이미 있으면 차원만 확인한다.

Before / 준비: .env 에 PINECONE_API_KEY (PINECONE_INDEX, EMBEDDING_DIM 은 선택)
Run / 실행:  python scripts/setup_pinecone.py
"""

import pathlib
import re
import sys
import time

from pinecone import Pinecone, ServerlessSpec

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"


def env_get(name: str, default: str = "") -> str:
    m = re.search(rf"^{name}=(.*)$", ENV.read_text(), re.M) if ENV.exists() else None
    return (m.group(1).strip() if m else "") or default


def main() -> None:
    key = env_get("PINECONE_API_KEY")
    if not key:
        sys.exit("PINECONE_API_KEY is missing in .env / .env 에 PINECONE_API_KEY 가 없음")
    name = env_get("PINECONE_INDEX", "wrist-rag")
    dim = int(env_get("EMBEDDING_DIM", "768"))
    pc = Pinecone(api_key=key)

    if pc.has_index(name):
        have = pc.describe_index(name).dimension
        if have != dim:
            sys.exit(f"index '{name}' exists with dimension {have}, but EMBEDDING_DIM={dim}. "
                     f"Use another PINECONE_INDEX name / 다른 이름을 써줘")
        print(f"index '{name}' already exists ({dim}d) / 이미 있음")
        return

    # 무료(Starter) 플랜은 AWS us-east-1 서버리스만 된다
    pc.create_index(name=name, dimension=dim, metric="cosine",
                    spec=ServerlessSpec(cloud="aws", region="us-east-1"))
    for _ in range(30):
        if pc.describe_index(name).status.ready:
            print(f"index '{name}' created ({dim}d, cosine) / 생성 완료")
            return
        time.sleep(2)
    sys.exit("index created but not ready yet. Check the Pinecone console / 아직 준비 중")


if __name__ == "__main__":
    main()
