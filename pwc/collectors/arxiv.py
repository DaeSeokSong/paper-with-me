"""arXiv API 수집기 — 최신 ML 분야 논문."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator

from .. import sources

API = "https://export.arxiv.org/api/query"
CATEGORIES = ["cs.LG", "cs.CV", "cs.CL", "cs.AI", "cs.NE", "cs.RO", "stat.ML"]
ATOM = "{http://www.w3.org/2005/Atom}"
# arXiv API의 실질 페이지네이션 상한 — 이보다 큰 범위는 창을 쪼갠다
PAGE_LIMIT = 30_000

PAPER_URL_PREFIX = "https://paperswithcode.com/paper/"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _fetch(url: str) -> list[dict]:
    with sources.open_with_retry(url, timeout=120) as resp:
        return parse_feed(resp.read())


def fetch_recent(max_results: int = 500) -> list[dict]:
    """ML 카테고리 논문을 최근 갱신순으로 가져온다.

    submittedDate 정렬은 개정판(v2+)이 수집 창에 들어오지 않는다 —
    lastUpdatedDate 정렬이 신규 제출과 개정을 모두 커버한다.
    """
    query = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    url = (f"{API}?search_query={urllib.parse.quote(query)}"
           f"&sortBy=lastUpdatedDate&sortOrder=descending"
           f"&start=0&max_results={max_results}")
    return _fetch(url)


def fetch_window(start: str, end: str, page_size: int = 1000,
                 delay: float = 3.0) -> Iterator[list[dict]]:
    """제출일 [start, end] 범위(YYYYMMDDHHMM)를 페이지 단위로 순회한다.

    arXiv API 예절(요청 간 3초)과 페이지네이션 상한을 지킨다 — 상한을
    넘길 만큼 큰 범위는 호출측(backfill)이 창을 쪼개 부른다."""
    cats = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    query = f"({cats}) AND submittedDate:[{start} TO {end}]"
    offset = 0
    while offset <= PAGE_LIMIT:
        url = (f"{API}?search_query={urllib.parse.quote(query)}"
               f"&sortBy=submittedDate&sortOrder=ascending"
               f"&start={offset}&max_results={page_size}")
        papers = _fetch(url)
        if not papers:
            return
        yield papers
        if len(papers) < page_size:
            return
        offset += page_size
        time.sleep(delay)


def backfill(conn: sqlite3.Connection, start_date: str, end_date: str,
             window_days: int = 14) -> int:
    """아카이브 종료(2025-07)와 일일 수집 시작 사이의 공백 기간을 채운다.

    일일 수집(fetch_recent)은 최근 논문만 가져오므로, 아카이브 이후
    수집 가동 전까지의 논문(약 1년치)은 이 백필 없이는 영구 공백이다 —
    'CIFAR-100을 쓴 2025-08~2026-06 논문이 하나도 없어 보이는' 원인.
    upsert가 arxiv_id 기준 멱등이라 재실행·기간 중복에 안전하다.
    """
    import datetime as dt

    cur = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    total = 0
    while cur <= end:
        win_end = min(cur + dt.timedelta(days=window_days - 1), end)
        received = 0
        for page in fetch_window(cur.strftime("%Y%m%d") + "0000",
                                 win_end.strftime("%Y%m%d") + "2359"):
            received += len(page)
            total += upsert_papers(conn, page, source="arxiv")
        print(f"[backfill] {cur} ~ {win_end}: 수신 {received:,}편 "
              f"(누적 신규 {total:,})", flush=True)
        cur = win_end + dt.timedelta(days=1)
    return total


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
            "updated": (entry.findtext(f"{ATOM}updated") or "")[:10] or None,
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
    taken_urls = {
        r[0] for r in conn.execute("SELECT paper_url FROM papers")
    }
    inserted = 0
    for p in papers:
        if not p.get("arxiv_id"):
            continue
        if p["arxiv_id"] in existing:
            # 개정판(v2+) 반영 — 수집 논문에 한해 제목/초록을 갱신한다
            # (아카이브 레코드는 원본 보존 우선)
            conn.execute(
                """UPDATE papers SET title = ?, abstract = ?, updated = ?
                   WHERE arxiv_id = ? AND source != 'archive'""",
                (p.get("title"), p.get("abstract"), p.get("updated"),
                 p["arxiv_id"]),
            )
            continue
        slug = slugify(p["title"])
        if not slug:
            continue
        # 제목 slug 충돌(동명 논문·아카이브 기존 slug) 시 arxiv_id로 유일화 —
        # OR IGNORE에 걸려 조용히 유실되는 것을 방지
        if PAPER_URL_PREFIX + slug in taken_urls:
            slug = f"{slug}-{slugify(p['arxiv_id'])}"
        cur = conn.execute(
            """INSERT OR IGNORE INTO papers
               (paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
                proceeding, date, updated, authors, tasks, methods, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                PAPER_URL_PREFIX + slug,
                p["arxiv_id"],
                p.get("title"),
                p.get("abstract"),
                p.get("url_abs"),
                p.get("url_pdf"),
                None,
                p.get("date"),
                p.get("updated"),
                json.dumps(p.get("authors") or [], ensure_ascii=False),
                json.dumps(p.get("tasks") or [], ensure_ascii=False),
                "[]",
                source,
            ),
        )
        if cur.rowcount == 0:
            print(f"[{source}] slug 충돌로 삽입 실패: {p['arxiv_id']} "
                  f"({slug})", flush=True)
            continue
        inserted += cur.rowcount
        existing.add(p["arxiv_id"])
        taken_urls.add(PAPER_URL_PREFIX + slug)
    conn.commit()
    return inserted


def collect(conn: sqlite3.Connection, max_results: int = 500) -> int:
    papers = fetch_recent(max_results)
    n = upsert_papers(conn, papers, source="arxiv")
    print(f"[arxiv] 수신 {len(papers)}편, 신규 {n}편", flush=True)
    return n
