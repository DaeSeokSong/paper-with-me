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
CREATE INDEX IF NOT EXISTS idx_sota_paper ON sota_rows(paper_url);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Phase 2: 실시간 인기 신호 (아카이브에는 없는 데이터)
CREATE TABLE IF NOT EXISTS signals (
    paper_url    TEXT PRIMARY KEY,
    github_stars INTEGER,
    hf_upvotes   INTEGER,
    updated_at   TEXT
);

-- GitHub 저장소 검색을 이미 수행한 논문 기록 — 0건 논문이 매일 같은
-- 검색을 재소모하며 백로그를 막는 것을 방지한다
CREATE TABLE IF NOT EXISTS repo_search_log (
    paper_url   TEXT PRIMARY KEY,
    searched_at TEXT
);
"""

# 기존 스냅샷 DB에도 적용되는 컬럼 추가. ALTER는 IF NOT EXISTS가 없으므로
# 중복 컬럼 오류를 무시하는 방식으로 멱등하게 실행한다.
MIGRATIONS = [
    "ALTER TABLE papers ADD COLUMN source TEXT DEFAULT 'archive'",
    "ALTER TABLE repos ADD COLUMN source TEXT DEFAULT 'archive'",
    "ALTER TABLE repos ADD COLUMN stars INTEGER",
    # 원본 evaluation-tables의 지표 순서(주 지표가 첫 번째). 리더보드 컬럼
    # 선택이 빈도 추정이 아닌 원본 정보를 쓰도록 보존한다.
    "ALTER TABLE sota_rows ADD COLUMN metrics_order TEXT",
]

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, abstract, content='papers', content_rowid='rowid'
);

-- 외부 콘텐츠 FTS는 자동 동기화가 없다. 트리거가 없으면 수집기가 나중에
-- 넣는 논문이 검색에서 영구 누락되고, OR REPLACE의 rowid 재배정으로
-- 인덱스가 오염되어 오검색까지 발생한다 (FTS5 공식 동기화 패턴).
CREATE TRIGGER IF NOT EXISTS papers_fts_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, abstract)
    VALUES (new.rowid, new.title, new.abstract);
END;
CREATE TRIGGER IF NOT EXISTS papers_fts_ad AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, abstract)
    VALUES ('delete', old.rowid, old.title, old.abstract);
END;
CREATE TRIGGER IF NOT EXISTS papers_fts_au AFTER UPDATE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, abstract)
    VALUES ('delete', old.rowid, old.title, old.abstract);
    INSERT INTO papers_fts(rowid, title, abstract)
    VALUES (new.rowid, new.title, new.abstract);
END;
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
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            # 멱등 재실행(중복 컬럼)만 무시하고 실제 오류는 드러낸다
            if "duplicate column name" not in str(e):
                raise
    conn.commit()
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


def sync_fts(conn: sqlite3.Connection) -> int:
    """FTS 인덱스에 빠진 논문을 증분 반영한다.

    동기화 트리거 도입 전에 수집된 스냅샷(트리거 없이 INSERT된 행)을
    복구하는 용도. 트리거가 있는 DB에서는 no-op이다.
    """
    if not has_fts(conn):
        return 0
    # 외부 콘텐츠 FTS는 `SELECT rowid FROM papers_fts`가 원본 테이블을 그대로
    # 비추므로, 실제 색인된 문서 목록은 내부 docsize 테이블에서 읽는다.
    cur = conn.execute(
        """INSERT INTO papers_fts(rowid, title, abstract)
           SELECT p.rowid, p.title, p.abstract FROM papers p
           WHERE p.rowid NOT IN (SELECT id FROM papers_fts_docsize)"""
    )
    conn.commit()
    return cur.rowcount
