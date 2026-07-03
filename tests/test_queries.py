import json
from pathlib import Path

import pytest

from app import queries
from pwc import db, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "pwc.sqlite")
    c.row_factory = __import__("sqlite3").Row
    yield c
    c.close()


def _fill_board(conn, task: str, dataset: str, n: int) -> None:
    rows = [
        (task, None, dataset, f"model-{i}",
         json.dumps({"Accuracy": f"{i / 10:.1f}"}), f"https://x/paper/p{i}",
         f"P{i}", "2020-01-01", "[]")
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_task_leaderboards_caps_rows_and_reports_total(conn):
    _fill_board(conn, "Big Task", "DS", 150)
    boards = queries.task_leaderboards(conn, "Big Task", limit=100)
    assert boards[0]["total"] == 150
    assert len(boards[0]["rows"]) == 100
    # 첫 지표 내림차순 정렬 확인
    assert boards[0]["rows"][0]["model_name"] == "model-149"

    full = queries.task_leaderboards(conn, "Big Task", limit=None)
    assert len(full[0]["rows"]) == 150


def test_task_leaderboards_single_query_grouping(conn):
    _fill_board(conn, "T", "DS-b", 3)
    _fill_board(conn, "T", "DS-a", 2)
    boards = queries.task_leaderboards(conn, "T")
    assert [b["dataset"] for b in boards] == ["DS-a", "DS-b"]
    assert [b["total"] for b in boards] == [2, 3]


def test_find_task_uses_cache(conn):
    _fill_board(conn, "Semantic Segmentation", "DS", 1)
    assert queries.find_task(conn, "semantic-segmentation") == "Semantic Segmentation"
    # 캐시 적중 (DB를 비워도 같은 결과)
    conn.execute("DELETE FROM sota_rows")
    conn.commit()
    assert queries.find_task(conn, "semantic-segmentation") == "Semantic Segmentation"
    assert queries.find_task(conn, "no-such-task") is None


def test_get_paper_exact_match_fast_path(conn):
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    p = queries.get_paper(conn, "attention-is-all-you-need")
    assert p and p["arxiv_id"] == "1706.03762"
