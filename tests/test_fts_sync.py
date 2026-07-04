"""FTS 동기화 회귀 테스트 — 검수에서 발견된 치명 버그.

수집기가 나중에 삽입하는 논문이 rebuild 없이도 검색에 잡혀야 하고(트리거),
트리거 도입 전 스냅샷의 유입분은 sync_fts로 복구돼야 한다.
"""

import sqlite3
from pathlib import Path

import pytest

from app import queries
from pwc import db, ingest
from pwc.collectors import arxiv

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "pwc.sqlite")
    c.row_factory = sqlite3.Row
    ingest.ingest_papers(c, FIXTURES / "papers.json")
    yield c
    c.close()


def test_collected_papers_are_searchable_without_rebuild(conn):
    if not db.has_fts(conn):
        pytest.skip("FTS 미지원 빌드")
    papers = [{
        "arxiv_id": "2507.99999",
        "title": "Quantum Zebra Networks",
        "abstract": "A watermelon zebrafish study.",
        "authors": ["Z. Author"], "date": "2026-07-03",
        "url_abs": "https://arxiv.org/abs/2507.99999",
        "url_pdf": "https://arxiv.org/pdf/2507.99999",
    }]
    assert arxiv.upsert_papers(conn, papers) == 1
    results = queries.search_papers(conn, "zebra")
    assert any(p["arxiv_id"] == "2507.99999" for p in results)


def test_reingest_does_not_corrupt_fts(conn):
    """OR REPLACE의 rowid 재배정이 FTS를 오염시키던 회귀 — UPSERT + 트리거로
    재적재 후에도 검색이 정확해야 한다."""
    if not db.has_fts(conn):
        pytest.skip("FTS 미지원 빌드")
    ingest.ingest_papers(conn, FIXTURES / "papers.json")  # 재적재
    results = queries.search_papers(conn, "attention")
    assert [p["title"] for p in results] == ["Attention Is All You Need"]


def test_sync_fts_repairs_pre_trigger_rows(conn):
    """트리거 도입 전 스냅샷 시뮬레이션: 트리거를 지우고 삽입된 행을
    sync_fts가 증분 복구한다."""
    if not db.has_fts(conn):
        pytest.skip("FTS 미지원 빌드")
    for t in ("papers_fts_ai", "papers_fts_ad", "papers_fts_au"):
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute(
        "INSERT INTO papers (paper_url, title, abstract) VALUES (?,?,?)",
        ("https://paperswithcode.com/paper/orphan-paper", "Orphan Paper", "lost"),
    )
    conn.commit()

    def fts_hits():
        return conn.execute(
            "SELECT rowid FROM papers_fts WHERE papers_fts MATCH 'orphan'"
        ).fetchall()

    assert fts_hits() == []  # 트리거 없이 삽입된 행은 인덱스에 없음
    assert db.sync_fts(conn) == 1
    assert len(fts_hits()) == 1
    assert db.sync_fts(conn) == 0  # 멱등
