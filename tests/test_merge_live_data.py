"""재빌드 시 수집 누적분 이관(merge_live_data) 테스트 — 데이터 소실 체인 방지."""

import json
import sqlite3
from pathlib import Path

import pytest

from pwc import db, ingest
from scripts.merge_live_data import merge

FIXTURES = Path(__file__).parent / "fixtures"


def _make_db(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.row_factory = sqlite3.Row
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    return conn


def test_merge_carries_collected_data_into_rebuild(tmp_path):
    # 이전 스냅샷: 아카이브 + 수집 논문/링크/신호
    old = _make_db(tmp_path / "old.sqlite")
    old.execute(
        "INSERT INTO papers (paper_url, arxiv_id, title, date, source) "
        "VALUES (?,?,?,?,?)",
        ("https://paperswithcode.com/paper/fresh-paper", "2507.12345",
         "Fresh Paper", "2026-07-01", "arxiv"),
    )
    old.execute(
        "INSERT INTO repos (paper_url, repo_url, source, stars) VALUES (?,?,?,?)",
        ("https://paperswithcode.com/paper/fresh-paper",
         "https://github.com/x/fresh", "github", 42),
    )
    old.execute(
        "INSERT INTO signals (paper_url, hf_upvotes, updated_at) VALUES (?,?,?)",
        ("https://paperswithcode.com/paper/fresh-paper", 7, "2026-07-01T00:00:00"),
    )
    old.commit()
    old.close()

    # 새 재빌드: 아카이브만 존재
    new = _make_db(tmp_path / "new.sqlite")
    new.close()

    counts = merge(tmp_path / "new.sqlite", tmp_path / "old.sqlite")
    assert counts == {"papers": 1, "repos": 1, "signals": 1}

    conn = db.connect(tmp_path / "new.sqlite")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source FROM papers WHERE arxiv_id='2507.12345'"
    ).fetchone()
    assert row["source"] == "arxiv"
    stars = conn.execute(
        "SELECT stars FROM repos WHERE repo_url='https://github.com/x/fresh'"
    ).fetchone()["stars"]
    assert stars == 42
    # 이관 후 검색도 동작 (FTS 반영)
    if db.has_fts(conn):
        from app import queries
        assert any(p["arxiv_id"] == "2507.12345"
                   for p in queries.search_papers(conn, "fresh"))
    conn.close()


def test_merge_skips_papers_already_in_archive(tmp_path):
    old = _make_db(tmp_path / "old.sqlite")
    # 아카이브에 이미 있는 arxiv_id(1706.03762)를 가진 수집 레코드
    old.execute(
        "INSERT INTO papers (paper_url, arxiv_id, title, source) VALUES (?,?,?,?)",
        ("https://paperswithcode.com/paper/attention-dup", "1706.03762",
         "Attention Dup", "hf"),
    )
    old.commit()
    old.close()
    new = _make_db(tmp_path / "new.sqlite")
    new.close()

    counts = merge(tmp_path / "new.sqlite", tmp_path / "old.sqlite")
    assert counts["papers"] == 0
