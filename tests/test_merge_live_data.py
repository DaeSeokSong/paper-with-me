"""재빌드 시 수집 누적분 이관(merge_live_data) 테스트 — 데이터 소실 체인 방지."""

import sqlite3
import sys
from pathlib import Path

from pwc import db, ingest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.merge_live_data import merge  # noqa: E402

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
    assert counts == {"papers": 1, "repos": 1, "signals": 1,
                      "sota_rows": 0, "repo_search_log": 0,
                      "model_search_log": 0}

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


def test_merge_carries_contrib_auto_rows_and_search_logs(tmp_path):
    """재빌드 이관이 커뮤니티 기여·자동 추출 리더보드 행과 검색 이력을
    보존한다 — 누락되면 기여는 공백, auto는 영구 유실, 쿼터는 재소모
    (코드 리뷰에서 발견된 데이터 유실 체인)."""
    import json as _json

    old_db = tmp_path / "old.sqlite"
    conn = db.connect(old_db)
    for source, model in (("contrib", "ContribNet"), ("auto", "AutoNet"),
                          ("archive", "OldArchive")):
        conn.execute(
            "INSERT INTO sota_rows (task,dataset,model_name,metrics,"
            "paper_url,code_links,source) VALUES (?,?,?,?,?,?,?)",
            ("T", "DS", model, _json.dumps({"Acc": "90"}),
             f"https://x/paper/{model.lower()}", "[]", source),
        )
    conn.execute("INSERT INTO repo_search_log VALUES (?, ?)",
                 ("https://x/paper/p", "2026-01-01"))
    conn.commit(); conn.close()

    new_db = tmp_path / "new.sqlite"
    db.connect(new_db).close()
    counts = merge(new_db, old_db)
    assert counts["sota_rows"] == 2  # contrib + auto (archive 제외)
    assert counts["repo_search_log"] == 1

    check = db.connect(new_db)
    sources = {r[0] for r in check.execute(
        "SELECT source FROM sota_rows")}
    assert sources == {"contrib", "auto"}
    check.close()
