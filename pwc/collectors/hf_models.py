"""HF 모델 링크 수집기 — 논문 ↔ Hugging Face 모델 매칭.

HF Hub는 model card의 arXiv 인용을 색인하므로 `filter=arxiv:{id}`로
논문의 공식/커뮤니티 구현 모델을 정확히 찾을 수 있다 (GitHub 검색보다
오탐이 훨씬 적다). 모델은 repos에 source='hf'로 적재되어 논문 페이지의
구현 목록에 함께 노출된다.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://huggingface.co/api/models"


def fetch_models(arxiv_id: str, limit: int = 5) -> list[dict]:
    url = (f"{API}?filter={urllib.parse.quote('arxiv:' + arxiv_id)}"
           f"&sort=likes&direction=-1&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "paper-with-me/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return parse_models(resp.read())


def parse_models(data: bytes) -> list[dict]:
    return [
        {"model_id": m.get("id") or m.get("modelId"),
         "likes": m.get("likes") or 0}
        for m in json.loads(data)
        if m.get("id") or m.get("modelId")
    ]


def apply(conn: sqlite3.Connection, paper_url: str, models: list[dict]) -> int:
    inserted = 0
    for m in models:
        cur = conn.execute(
            """INSERT OR IGNORE INTO repos
               (paper_url, repo_url, is_official, framework,
                mentioned_in_paper, mentioned_in_github, source, stars)
               VALUES (?,?,?,?,?,?,?,?)""",
            (paper_url, f"https://huggingface.co/{m['model_id']}",
             None, None, None, None, "hf", m.get("likes")),
        )
        inserted += cur.rowcount
    return inserted


def papers_needing_models(conn: sqlite3.Connection, limit: int = 50) -> list[tuple]:
    return conn.execute(
        """SELECT paper_url, arxiv_id FROM papers p
           WHERE p.source != 'archive' AND p.arxiv_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM model_search_log l
                             WHERE l.paper_url = p.paper_url)
           ORDER BY p.date DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def collect(conn: sqlite3.Connection, max_papers: int = 50,
            delay: float = 0.5) -> int:
    total = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    targets = papers_needing_models(conn, max_papers)
    for paper_url, arxiv_id in targets:
        try:
            models = fetch_models(arxiv_id)
        except Exception as e:  # noqa: BLE001 - 개별 실패는 보고 후 계속
            print(f"[hf-models] {arxiv_id} 조회 실패: {e}", flush=True)
            continue
        n = apply(conn, paper_url, models)
        conn.execute(
            "INSERT OR REPLACE INTO model_search_log (paper_url, searched_at) "
            "VALUES (?,?)", (paper_url, now),
        )
        total += n
        if models:
            print(f"[hf-models] {arxiv_id}: 모델 {n}개", flush=True)
        time.sleep(delay)
    conn.commit()
    print(f"[hf-models] 논문 {len(targets)}편 조회, 모델 링크 {total}건 추가",
          flush=True)
    return total
