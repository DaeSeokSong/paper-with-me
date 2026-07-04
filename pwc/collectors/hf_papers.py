"""Hugging Face Daily Papers 수집기 — 신규 논문 + 업보트 신호."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone

from . import arxiv

API = "https://huggingface.co/api/daily_papers"


def fetch_daily(limit: int = 100) -> list[dict]:
    req = urllib.request.Request(f"{API}?limit={limit}",
                                 headers={"User-Agent": "paper-with-me/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return parse_daily(resp.read())


def parse_daily(data: bytes) -> list[dict]:
    """daily_papers 응답을 papers 포맷 + upvotes로 변환한다."""
    items = json.loads(data)
    papers = []
    for item in items:
        p = item.get("paper") or {}
        arxiv_id = p.get("id")
        if not arxiv_id:
            continue
        papers.append({
            "arxiv_id": arxiv_id,
            "title": " ".join((p.get("title") or "").split()),
            "abstract": " ".join((p.get("summary") or "").split()),
            "authors": [a.get("name") for a in p.get("authors") or []
                        if a.get("name")],
            "date": (p.get("publishedAt") or "")[:10],
            "url_abs": f"https://arxiv.org/abs/{arxiv_id}",
            "url_pdf": f"https://arxiv.org/pdf/{arxiv_id}",
            "upvotes": p.get("upvotes") or 0,
        })
    return papers


def apply(conn: sqlite3.Connection, papers: list[dict]) -> tuple[int, int]:
    """신규 논문 삽입 + 업보트 신호 갱신. (신규 논문 수, 신호 갱신 수) 반환."""
    inserted = arxiv.upsert_papers(conn, papers, source="hf")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = 0
    for p in papers:
        row = conn.execute(
            "SELECT paper_url FROM papers WHERE arxiv_id = ?", (p["arxiv_id"],)
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            """INSERT INTO signals (paper_url, hf_upvotes, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(paper_url) DO UPDATE SET
                 hf_upvotes = excluded.hf_upvotes,
                 updated_at = excluded.updated_at""",
            (row[0], p.get("upvotes") or 0, now),
        )
        updated += 1
    conn.commit()
    return inserted, updated


def collect(conn: sqlite3.Connection, limit: int = 100) -> int:
    papers = fetch_daily(limit)
    inserted, updated = apply(conn, papers)
    print(f"[hf] 수신 {len(papers)}편, 신규 {inserted}편, 신호 갱신 {updated}건",
          flush=True)
    return inserted
