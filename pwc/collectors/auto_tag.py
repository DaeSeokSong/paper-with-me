"""수집 논문 자동 태깅 — 제목·초록에서 알려진 task명을 탐지해 부여한다.

arXiv 수집 논문은 tasks가 비어 있어 카드에 태그가 없고, 다이제스트
분야 분류·태그 검색에서도 빠진다. 아카이브 리더보드의 task 어휘
(2,000여 종)를 사전으로 써서 본문에 실제로 언급된 task만 태그로 단다.

오탐 방지:
- 두 단어 이상의 task명만 사용 ("Classification" 같은 단독 일반어 제외)
- 단어 경계 일치, 대소문자 무시
- 논문당 최대 4개, 긴(구체적인) 이름 우선
"""

from __future__ import annotations

import json
import re
import sqlite3

MAX_TAGS = 4


def _vocabulary(conn: sqlite3.Connection) -> list[str]:
    names = [
        t for (t,) in conn.execute(
            "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL")
        if isinstance(t, str) and len(t.split()) >= 2
    ]
    # 긴 이름 우선 — "Few-Shot Image Classification"이 "Image
    # Classification"보다 먼저 매치되도록
    names.sort(key=len, reverse=True)
    return names


def tag_text(title: str, abstract: str, vocab: list[str]) -> list[str]:
    text = f"{title or ''} {abstract or ''}".lower()
    tags: list[str] = []
    for name in vocab:
        if len(tags) >= MAX_TAGS:
            break
        if re.search(rf"\b{re.escape(name.lower())}\b", text):
            # 이미 잡힌 더 구체적인 태그의 부분 문자열이면 건너뛴다
            if any(name.lower() in t.lower() for t in tags):
                continue
            tags.append(name)
    return tags


def collect(conn: sqlite3.Connection, max_papers: int = 2000) -> int:
    """tasks가 빈 수집 논문에 태그를 부여한다 (이미 태그된 논문은 유지)."""
    papers = conn.execute(
        """SELECT paper_url, title, abstract FROM papers
           WHERE source != 'archive'
             AND (tasks IS NULL OR tasks = '[]' OR tasks = '')
           ORDER BY date DESC LIMIT ?""",
        (max_papers,),
    ).fetchall()
    if not papers:
        print("[tags] 태깅 대상 없음", flush=True)
        return 0
    vocab = _vocabulary(conn)
    tagged = 0
    for paper_url, title, abstract in papers:
        tags = tag_text(title, abstract, vocab)
        if not tags:
            continue
        conn.execute(
            "UPDATE papers SET tasks = ? WHERE paper_url = ?",
            (json.dumps(tags, ensure_ascii=False), paper_url),
        )
        tagged += 1
    conn.commit()
    # 태그가 새로 붙었으면 태그 검색 역인덱스도 재구축되도록 플래그 무효화
    if tagged:
        conn.execute("DELETE FROM meta WHERE key = 'papers_tasks_built'")
        conn.commit()
    print(f"[tags] {len(papers)}편 검사, {tagged}편 태깅", flush=True)
    return tagged
