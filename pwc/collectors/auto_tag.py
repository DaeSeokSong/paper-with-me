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
    # 자동 추출(auto) 행이 만든 task명이 다시 태깅 어휘가 되면 오염이
    # 자기 강화된다 — results_extract의 sanity 기준과 같은 원칙으로 제외
    names = [
        t for (t,) in conn.execute(
            """SELECT DISTINCT task FROM sota_rows
               WHERE task IS NOT NULL
                 AND (source IS NULL OR source != 'auto')""")
        if isinstance(t, str) and len(t.split()) >= 2
    ]
    # 긴 이름 우선 — "Few-Shot Image Classification"이 "Image
    # Classification"보다 먼저 매치되도록
    names.sort(key=len, reverse=True)
    return names


def _matcher(vocab: list[str]) -> re.Pattern:
    """어휘 전체를 하나의 교대 정규식으로 결합 — 논문당 단일 스캔.

    이름별 개별 re.search(어휘 ~1,600종 × 논문 수)는 백필처럼 수십만
    편을 일괄 태깅할 때 시간이 폭발한다. 교대는 길이순(구체명 우선)
    정렬된 vocab 순서를 그대로 써서 같은 위치에서 긴 이름이 이긴다."""
    pat = "|".join(re.escape(n.lower()) for n in vocab) or r"(?!x)x"
    return re.compile(rf"\b(?:{pat})\b")


def tag_text(title: str, abstract: str, vocab: list[str],
             matcher: re.Pattern | None = None) -> list[str]:
    text = f"{title or ''} {abstract or ''}".lower()
    if matcher is None:
        matcher = _matcher(vocab)
    found = {m.group(0) for m in matcher.finditer(text)}
    if not found:
        return []
    canon = {n.lower(): n for n in vocab}
    tags: list[str] = []
    # 길고 구체적인 이름 우선, 그 부분 문자열인 일반명은 제외 (기존 의미)
    for low in sorted(found, key=len, reverse=True):
        name = canon.get(low)
        if name is None or any(low in t.lower() for t in tags):
            continue
        tags.append(name)
        if len(tags) >= MAX_TAGS:
            break
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
    matcher = _matcher(vocab)
    tagged = 0
    for paper_url, title, abstract in papers:
        tags = tag_text(title, abstract, vocab, matcher)
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
