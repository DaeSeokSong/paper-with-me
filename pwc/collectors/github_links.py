"""GitHub 수집기 — 신규 논문의 코드 저장소 매칭 + 스타 신호.

GitHub 저장소 검색(이름/설명/README 대상)으로 arXiv ID를 언급하는 저장소를
찾는다. API rate limit(검색 30회/분)을 고려해 실행당 논문 수를 제한한다.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com/search/repositories"


def search_repos(arxiv_id: str, token: str | None = None) -> list[dict]:
    headers = {
        "User-Agent": "paper-with-me/0.1",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # in:readme 포함 — 진짜 구현 저장소는 README에만 arXiv ID를 적는 경우가
    # 많다 (기본 검색 필드는 name/description). fork는 원본만 취한다.
    query = f'"{arxiv_id}" in:name,description,readme fork:false'
    url = f"{API}?q={urllib.parse.quote(query)}&per_page=3&sort=stars"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return parse_search(resp.read())


def parse_search(data: bytes) -> list[dict]:
    items = json.loads(data).get("items") or []
    return [
        {
            "repo_url": r.get("html_url"),
            "stars": r.get("stargazers_count") or 0,
            # GitHub language(python 등)는 프레임워크(pytorch/tf)가 아니다 —
            # framework 컬럼에 넣으면 /trends 점유율이 오염된다
            "language": None,
        }
        for r in items
        if r.get("html_url") and not r.get("fork")
    ]


def apply(conn: sqlite3.Connection, paper_url: str, repos: list[dict]) -> int:
    """검색 결과를 repos에 적재하고 signals.github_stars를 갱신한다."""
    inserted = 0
    for r in repos:
        cur = conn.execute(
            """INSERT OR IGNORE INTO repos
               (paper_url, repo_url, is_official, framework,
                mentioned_in_paper, mentioned_in_github, source, stars)
               VALUES (?,?,?,?,?,?,?,?)""",
            (paper_url, r["repo_url"], None, r.get("language"),
             None, 1, "github", r.get("stars")),
        )
        inserted += cur.rowcount
    if repos:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO signals (paper_url, github_stars, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(paper_url) DO UPDATE SET
                 github_stars = excluded.github_stars,
                 updated_at = excluded.updated_at""",
            (paper_url, max(r["stars"] for r in repos), now),
        )
    return inserted


def papers_needing_repos(conn: sqlite3.Connection, limit: int = 25) -> list[tuple]:
    """아직 GitHub 검색을 시도하지 않은 신규(비아카이브) 논문, 최신순.

    repos 존재 여부가 아니라 검색 이력(repo_search_log) 기준 — 0건 논문이
    매일 같은 검색을 재소모하는 것을 막는다.
    """
    return conn.execute(
        """SELECT paper_url, arxiv_id FROM papers p
           WHERE p.source != 'archive' AND p.arxiv_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM repo_search_log l
                             WHERE l.paper_url = p.paper_url)
           ORDER BY p.date DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def collect(conn: sqlite3.Connection, token: str | None = None,
            max_papers: int = 50, delay: float = 2.5) -> int:
    total = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    targets = papers_needing_repos(conn, max_papers)
    for paper_url, arxiv_id in targets:
        try:
            repos = search_repos(arxiv_id, token)
        except Exception as e:  # noqa: BLE001 - 개별 실패는 보고 후 계속
            print(f"[github] {arxiv_id} 검색 실패: {e}", flush=True)
            continue
        n = apply(conn, paper_url, repos)
        conn.execute(
            "INSERT OR REPLACE INTO repo_search_log (paper_url, searched_at) "
            "VALUES (?,?)", (paper_url, now),
        )
        total += n
        if repos:
            print(f"[github] {arxiv_id}: 저장소 {n}개", flush=True)
        time.sleep(delay)  # 검색 API rate limit(30회/분) 준수
    conn.commit()
    print(f"[github] 논문 {len(targets)}편 검색, 링크 {total}건 추가", flush=True)
    return total
