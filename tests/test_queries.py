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


def test_task_benchmarks_lists_datasets_without_rows(conn):
    _fill_board(conn, "T", "DS-b", 3)
    _fill_board(conn, "T", "DS-a", 2)
    benchmarks = queries.task_benchmarks(conn, "T")
    assert [b["dataset"] for b in benchmarks] == ["DS-b", "DS-a"]  # 결과 수 내림차순
    assert [b["n_rows"] for b in benchmarks] == [3, 2]
    assert benchmarks[0]["slug"] == "ds-b"


def test_dataset_leaderboard_sorted_by_first_metric(conn):
    _fill_board(conn, "Big Task", "DS", 150)
    board = queries.dataset_leaderboard(conn, "Big Task", "DS")
    assert len(board["rows"]) == 150
    assert board["rows"][0]["model_name"] == "model-149"  # 첫 지표 내림차순
    assert board["metric_names"] == ["Accuracy"]


def test_find_benchmark_dataset(conn):
    _fill_board(conn, "T", "Mini-ImageNet 5-way (1-shot)", 1)
    ds = queries.find_benchmark_dataset(conn, "T", "mini-imagenet-5-way-1-shot")
    assert ds == "Mini-ImageNet 5-way (1-shot)"
    assert queries.find_benchmark_dataset(conn, "T", "nope") is None


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
