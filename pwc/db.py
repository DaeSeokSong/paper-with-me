"""SQLite 스키마 및 연결 헬퍼."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_url   TEXT PRIMARY KEY,
    arxiv_id    TEXT,
    title       TEXT,
    abstract    TEXT,
    url_abs     TEXT,
    url_pdf     TEXT,
    proceeding  TEXT,
    date        TEXT,
    authors     TEXT,  -- JSON 배열
    tasks       TEXT,  -- JSON 배열
    methods     TEXT   -- JSON 배열
);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_date ON papers(date);

CREATE TABLE IF NOT EXISTS repos (
    paper_url           TEXT,
    repo_url            TEXT,
    is_official         INTEGER,
    framework           TEXT,
    mentioned_in_paper  INTEGER,
    mentioned_in_github INTEGER,
    PRIMARY KEY (paper_url, repo_url)
);
CREATE INDEX IF NOT EXISTS idx_repos_repo ON repos(repo_url);

CREATE TABLE IF NOT EXISTS datasets (
    url         TEXT PRIMARY KEY,
    name        TEXT,
    full_name   TEXT,
    homepage    TEXT,
    description TEXT,
    paper_url   TEXT,
    modalities  TEXT,  -- JSON 배열
    languages   TEXT,  -- JSON 배열
    num_papers  INTEGER
);

CREATE TABLE IF NOT EXISTS methods (
    url            TEXT PRIMARY KEY,
    name           TEXT,
    full_name      TEXT,
    description    TEXT,
    paper_url      TEXT,
    introduced_year INTEGER,
    source_url     TEXT,
    source_title   TEXT,
    num_papers     INTEGER,
    collections    TEXT  -- JSON 배열
);

CREATE TABLE IF NOT EXISTS sota_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT,
    parent_task TEXT,
    dataset     TEXT,
    model_name  TEXT,
    metrics     TEXT,  -- JSON 객체
    paper_url   TEXT,
    paper_title TEXT,
    paper_date  TEXT,
    code_links  TEXT   -- JSON 배열
);
CREATE INDEX IF NOT EXISTS idx_sota_task_dataset ON sota_rows(task, dataset);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, abstract, content='papers', content_rowid='rowid'
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        # FTS5 미지원 빌드에서는 전문 검색 없이 동작
        pass
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    return row is not None


def rebuild_fts(conn: sqlite3.Connection) -> None:
    if has_fts(conn):
        conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
        conn.commit()
