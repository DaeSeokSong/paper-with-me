import json
import sqlite3
from pathlib import Path

import pytest

from pwc import db, ingest
from pwc.collectors import arxiv, github_links, hf_papers

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "pwc.sqlite")
    c.row_factory = sqlite3.Row
    # 아카이브 상태 재현 (Attention Is All You Need 포함)
    ingest.ingest_papers(c, FIXTURES / "papers.json")
    yield c
    c.close()


def test_migrations_add_source_to_existing_db(conn):
    row = conn.execute(
        "SELECT source FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()
    assert row["source"] == "archive"


def test_arxiv_parse_feed_normalizes_entries():
    papers = arxiv.parse_feed((FIXTURES / "arxiv-feed.xml").read_bytes())
    assert len(papers) == 2
    p = papers[0]
    assert p["arxiv_id"] == "2507.11111"  # 버전 접미사 제거
    assert p["title"] == "Scaling Laws for Restored Leaderboards"  # 공백 정규화
    assert p["authors"] == ["Alice Kim", "Bob Lee"]
    assert p["date"] == "2026-07-01"


def test_arxiv_upsert_skips_archived_papers(conn):
    papers = arxiv.parse_feed((FIXTURES / "arxiv-feed.xml").read_bytes())
    assert arxiv.upsert_papers(conn, papers) == 1  # 중복(1706.03762) 제외
    row = conn.execute(
        "SELECT source, paper_url FROM papers WHERE arxiv_id='2507.11111'"
    ).fetchone()
    assert row["source"] == "arxiv"
    assert row["paper_url"].endswith("/paper/scaling-laws-for-restored-leaderboards")
    # 재실행해도 중복 삽입 없음
    assert arxiv.upsert_papers(conn, papers) == 0


def test_hf_apply_inserts_new_and_updates_signals(conn):
    arxiv.upsert_papers(
        conn, arxiv.parse_feed((FIXTURES / "arxiv-feed.xml").read_bytes())
    )
    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    inserted, updated = hf_papers.apply(conn, papers)
    assert inserted == 1  # 2507.22222만 신규
    assert updated == 2   # 두 논문 모두 업보트 신호 기록
    upvotes = dict(conn.execute(
        "SELECT p.arxiv_id, s.hf_upvotes FROM signals s "
        "JOIN papers p ON p.paper_url = s.paper_url"
    ).fetchall())
    assert upvotes == {"2507.22222": 42, "2507.11111": 7}


def test_github_apply_links_repos_and_stars(conn):
    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    hf_papers.apply(conn, papers)
    paper_url = conn.execute(
        "SELECT paper_url FROM papers WHERE arxiv_id='2507.22222'"
    ).fetchone()[0]

    repos = github_links.parse_search((FIXTURES / "github-search.json").read_bytes())
    assert github_links.apply(conn, paper_url, repos) == 2

    stars = conn.execute(
        "SELECT github_stars FROM signals WHERE paper_url=?", (paper_url,)
    ).fetchone()[0]
    assert stars == 512  # 최다 스타 저장소 기준


def test_repo_search_log_prevents_daily_requery(conn):
    """검색 0건 논문이 매일 같은 검색을 재소모하지 않도록, 재검색 대상은
    repos 존재가 아니라 검색 이력 기준이다."""
    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    hf_papers.apply(conn, papers)
    targets = github_links.papers_needing_repos(conn)
    assert targets  # 검색 이력이 없으므로 대상
    paper_url = targets[0][0]
    conn.execute(
        "INSERT INTO repo_search_log (paper_url, searched_at) VALUES (?,?)",
        (paper_url, "2026-07-04T00:00:00"),
    )
    conn.commit()
    # 0건이었어도(레포 미보유) 이력이 있으면 제외
    assert paper_url not in [t[0] for t in github_links.papers_needing_repos(conn)]


def test_github_language_not_stored_as_framework(conn):
    """GitHub language(python 등)는 프레임워크가 아니다 — trends 오염 방지."""
    repos = github_links.parse_search((FIXTURES / "github-search.json").read_bytes())
    assert all(r["language"] is None for r in repos)


def test_arxiv_revision_updates_collected_papers_only(conn):
    """개정판(v2+)은 수집 논문의 제목/초록만 갱신하고 아카이브는 보존한다."""
    papers = arxiv.parse_feed((FIXTURES / "arxiv-feed.xml").read_bytes())
    arxiv.upsert_papers(conn, papers)
    revised = [dict(papers[0], title="Scaling Laws v2 Revised",
                    updated="2026-07-04"),
               dict(papers[1], title="Attention Revised (should not apply)")]
    assert arxiv.upsert_papers(conn, revised) == 0  # 신규 삽입 없음
    row = conn.execute(
        "SELECT title, updated FROM papers WHERE arxiv_id='2507.11111'"
    ).fetchone()
    assert row["title"] == "Scaling Laws v2 Revised"
    assert row["updated"] == "2026-07-04"
    # 아카이브 논문은 개정 반영 대상이 아니다
    archived = conn.execute(
        "SELECT title FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()
    assert archived["title"] == "Attention Is All You Need"


def test_hf_models_parse_and_apply(conn):
    from pwc.collectors import hf_models

    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    hf_papers.apply(conn, papers)
    paper_url = conn.execute(
        "SELECT paper_url FROM papers WHERE arxiv_id='2507.22222'"
    ).fetchone()[0]

    models = hf_models.parse_models((FIXTURES / "hf-models.json").read_bytes())
    assert [m["model_id"] for m in models] == [
        "carolpark/diffusion-strike-back-base", "community/dsb-finetune"]
    assert hf_models.apply(conn, paper_url, models) == 2

    row = conn.execute(
        "SELECT repo_url, source, stars FROM repos WHERE source='hf' "
        "ORDER BY stars DESC"
    ).fetchone()
    assert row["repo_url"] == \
        "https://huggingface.co/carolpark/diffusion-strike-back-base"
    assert row["stars"] == 128

    # 검색 이력 기록 후 재조회 대상에서 제외
    conn.execute("INSERT INTO model_search_log (paper_url, searched_at) "
                 "VALUES (?, '2026-07-04T00:00:00')", (paper_url,))
    conn.commit()
    assert paper_url not in [
        t[0] for t in hf_models.papers_needing_models(conn)]


def test_trigram_fts_supports_partial_word_search(conn):
    """새 빌드의 trigram 토크나이저 — 한글·영문 부분어 검색."""
    from app import queries

    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='papers_fts'"
                       ).fetchone()
    if not sql or "trigram" not in sql[0]:
        pytest.skip("trigram 미적용 DB")
    conn.execute(
        "INSERT INTO papers (paper_url, title, abstract) VALUES (?,?,?)",
        ("https://paperswithcode.com/paper/korean-paper",
         "딥러닝 기반 영상 분류 연구", "한국어 초록"),
    )
    conn.commit()
    assert any("딥러닝" in p["title"]
               for p in queries.search_papers(conn, "러닝"))  # 부분어
    assert any("Attention" in p["title"]
               for p in queries.search_papers(conn, "ttention"))


def test_stale_signals_excluded_from_trending(conn):
    from app import queries

    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    hf_papers.apply(conn, papers)
    # 신호를 15일 전으로 되돌리면 (리스트 이탈 후 동결 시나리오) 트렌딩 제외
    conn.execute("UPDATE signals SET updated_at = datetime('now', '-15 days')")
    conn.commit()
    trending = queries.trending_papers(conn)
    assert all(p["arxiv_id"] != "2507.22222" for p in trending)


def test_trending_prefers_signal_papers(conn, tmp_path):
    from app import queries

    papers = hf_papers.parse_daily((FIXTURES / "hf-daily.json").read_bytes())
    hf_papers.apply(conn, papers)
    trending = queries.trending_papers(conn)
    assert trending[0]["arxiv_id"] == "2507.22222"  # 업보트 42가 최상단
    assert trending[0]["hf_upvotes"] == 42
