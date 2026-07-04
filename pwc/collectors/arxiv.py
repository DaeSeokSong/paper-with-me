"""arXiv API 수집기 — 최신 ML 분야 논문."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable

API = "https://export.arxiv.org/api/query"
CATEGORIES = ["cs.LG", "cs.CV", "cs.CL", "cs.AI", "cs.NE", "cs.RO", "stat.ML"]
ATOM = "{http://www.w3.org/2005/Atom}"

PAPER_URL_PREFIX = "https://paperswithcode.com/paper/"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def fetch_recent(max_results: int = 500) -> list[dict]:
    """최신 제출순으로 ML 카테고리 논문을 가져온다."""
    query = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    url = (f"{API}?search_query={urllib.parse.quote(query)}"
           f"&sortBy=submittedDate&sortOrder=descending"
           f"&start=0&max_results={max_results}")
    req = urllib.request.Request(url, headers={"User-Agent": "paper-with-me/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return parse_feed(resp.read())


def parse_feed(data: bytes) -> list[dict]:
    """arXiv Atom 피드를 파싱해 아카이브 papers 포맷의 레코드로 변환한다."""
    root = ET.fromstring(data)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = entry.findtext(f"{ATOM}id") or ""
        # http://arxiv.org/abs/2507.01234v2 -> 2507.01234
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        if not arxiv_id:
            continue
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        published = (entry.findtext(f"{ATOM}published") or "")[:10]
        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "abstract": " ".join((entry.findtext(f"{ATOM}summary") or "").split()),
            "authors": [
                a.findtext(f"{ATOM}name")
                for a in entry.findall(f"{ATOM}author")
                if a.findtext(f"{ATOM}name")
            ],
            "date": published,
            "url_abs": f"https://arxiv.org/abs/{arxiv_id}",
            "url_pdf": f"https://arxiv.org/pdf/{arxiv_id}",
        })
    return papers


def upsert_papers(conn: sqlite3.Connection, papers: Iterable[dict],
                  source: str = "arxiv") -> int:
    """새 논문만 삽입한다. arxiv_id가 이미 있으면 건너뛴다 (아카이브 우선)."""
    existing = {
        r[0] for r in conn.execute(
            "SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL"
        )
    }
    inserted = 0
    for p in papers:
        if not p.get("arxiv_id") or p["arxiv_id"] in existing:
            continue
        slug = slugify(p["title"])
        if not slug:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO papers
               (paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
                proceeding, date, authors, tasks, methods, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                PAPER_URL_PREFIX + slug,
                p["arxiv_id"],
                p.get("title"),
                p.get("abstract"),
                p.get("url_abs"),
                p.get("url_pdf"),
                None,
                p.get("date"),
                json.dumps(p.get("authors") or [], ensure_ascii=False),
                json.dumps(p.get("tasks") or [], ensure_ascii=False),
                "[]",
                source,
            ),
        )
        inserted += cur.rowcount
        existing.add(p["arxiv_id"])
    conn.commit()
    return inserted


def collect(conn: sqlite3.Connection, max_results: int = 500) -> int:
    papers = fetch_recent(max_results)
    n = upsert_papers(conn, papers, source="arxiv")
    print(f"[arxiv] 수신 {len(papers)}편, 신규 {n}편", flush=True)
    return n
