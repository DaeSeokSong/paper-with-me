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


def test_dataset_leaderboard_preserves_original_order(conn):
    """지표값 재정렬은 낮을수록 좋은 지표·표기 혼용에서 순위를 왜곡하므로
    원본 덤프의 큐레이션 순서(rowid)를 보존한다."""
    _fill_board(conn, "Big Task", "DS", 150)
    board = queries.dataset_leaderboard(conn, "Big Task", "DS")
    assert len(board["rows"]) == 150
    assert board["rows"][0]["model_name"] == "model-0"  # 원본 순서
    assert board["metric_names"] == ["Accuracy"]


def test_dataset_leaderboard_uses_original_metric_order(conn):
    """지표 컬럼은 원본 sota.metrics 순서(주 지표 우선)를 사용한다."""
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links,metrics_order) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("T", None, "DS", "M1",
         json.dumps({"Params (M)": "300", "Accuracy": "91"}),
         "https://x/paper/p1", "P1", "2020-01-01", "[]",
         json.dumps(["Accuracy", "Params (M)"])),
    )
    conn.commit()
    board = queries.dataset_leaderboard(conn, "T", "DS")
    assert board["metric_names"] == ["Accuracy", "Params (M)"]


def test_find_benchmark_dataset(conn):
    _fill_board(conn, "T", "Mini-ImageNet 5-way (1-shot)", 1)
    ds = queries.find_benchmark_dataset(conn, "T", "mini-imagenet-5-way-1-shot")
    assert ds == "Mini-ImageNet 5-way (1-shot)"
    assert queries.find_benchmark_dataset(conn, "T", "nope") is None


def test_dataset_leaderboard_cleans_null_filled_metrics(conn):
    """기존 스냅샷(정화 전 적재분) 대응: metrics에 null 채움 키가 있어도
    읽기 시점에 제거되어 지표 컬럼과 값이 올바르게 나와야 한다."""
    dirty = json.dumps({"Accuracy": "96.08", "Content Selection (F1)": None,
                        "Macro-F1": None, "Rank-1": None})
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links) VALUES (?,?,?,?,?,?,?,?,?)",
        ("Image Classification", None, "CIFAR-100", "EffNet-L2", dirty,
         "https://x/paper/p", "P", "2020-10-03", "[]"),
    )
    conn.commit()
    board = queries.dataset_leaderboard(conn, "Image Classification", "CIFAR-100")
    assert board["metric_names"] == ["Accuracy"]
    assert board["rows"][0]["metrics"] == {"Accuracy": "96.08"}


def test_find_task_cache_invalidates_on_snapshot_swap(conn, tmp_path):
    _fill_board(conn, "Semantic Segmentation", "DS", 1)
    assert queries.find_task(conn, "semantic-segmentation") == "Semantic Segmentation"
    assert queries.find_task(conn, "no-such-task") is None

    # 같은 경로에 스냅샷 교체(일일 갱신 시나리오) → 새 task가 보여야 한다
    db_path = tmp_path / "pwc.sqlite"
    conn.close()
    db_path.unlink()
    import sqlite3

    from pwc import db as pwc_db
    conn2 = pwc_db.connect(db_path)
    conn2.row_factory = sqlite3.Row
    _fill_board(conn2, "New Task", "DS", 1)
    assert queries.find_task(conn2, "new-task") == "New Task"
    assert queries.find_task(conn2, "semantic-segmentation") is None
    conn2.close()


def test_get_paper_rejects_wildcard_slugs(conn):
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    assert queries.get_paper(conn, "%") is None
    assert queries.get_paper(conn, "attention_is_all_you_need") is None
    assert queries.get_paper(conn, "attention-is-all-you-need") is not None


def test_get_paper_exact_match_fast_path(conn):
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    p = queries.get_paper(conn, "attention-is-all-you-need")
    assert p and p["arxiv_id"] == "1706.03762"
