import gzip
import json
import shutil
from pathlib import Path

import pytest

from pwc import db, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "pwc.sqlite")
    yield conn
    conn.close()


def test_ingest_papers(conn):
    n = ingest.ingest_papers(conn, FIXTURES / "papers.json")
    assert n == 2
    row = conn.execute(
        "SELECT title, arxiv_id, authors FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()
    assert row[0] == "Attention Is All You Need"
    assert "Ashish Vaswani" in json.loads(row[2])


def test_ingest_papers_gz(conn, tmp_path):
    gz = tmp_path / "papers.json.gz"
    with open(FIXTURES / "papers.json", "rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    assert ingest.ingest_papers(conn, gz) == 2


def test_ingest_links(conn):
    n = ingest.ingest_links(conn, FIXTURES / "links.json")
    assert n == 2
    official = conn.execute(
        "SELECT repo_url FROM repos WHERE is_official=1"
    ).fetchall()
    assert official == [("https://github.com/tensorflow/tensor2tensor",)]


def test_ingest_evaluations_flattens_subtasks(conn):
    n = ingest.ingest_evaluations(conn, FIXTURES / "evaluation-tables.json")
    assert n == 2  # 최상위 task 1행 + subtask 1행
    sub = conn.execute(
        "SELECT task, parent_task, dataset, model_name FROM sota_rows "
        "WHERE parent_task IS NOT NULL"
    ).fetchone()
    assert sub == (
        "Few-Shot Image Classification",
        "Image Classification",
        "Mini-ImageNet 5-way (1-shot)",
        "ProtoNet",
    )
    metrics = json.loads(conn.execute(
        "SELECT metrics FROM sota_rows WHERE model_name='ResNet-152'"
    ).fetchone()[0])
    assert metrics["Top 1 Accuracy"] == "78.57%"


def test_ingest_methods_and_datasets(conn):
    assert ingest.ingest_methods(conn, FIXTURES / "methods.json") == 1
    assert ingest.ingest_datasets(conn, FIXTURES / "datasets.json") == 1
    method = conn.execute(
        "SELECT introduced_year, paper_url FROM methods WHERE name='Transformer'"
    ).fetchone()
    assert method[0] == 2017
    assert method[1].endswith("/attention-is-all-you-need")


def test_ingest_all_and_fts(conn):
    counts = ingest.ingest_all(conn, {
        "papers": FIXTURES / "papers.json",
        "links": FIXTURES / "links.json",
        "evaluations": FIXTURES / "evaluation-tables.json",
        "methods": FIXTURES / "methods.json",
        "datasets": FIXTURES / "datasets.json",
    })
    assert counts == {
        "papers": 2, "links": 2, "evaluations": 2, "methods": 1, "datasets": 1,
    }
    if db.has_fts(conn):
        hits = conn.execute(
            "SELECT title FROM papers_fts WHERE papers_fts MATCH 'attention'"
        ).fetchall()
        assert ("Attention Is All You Need",) in hits


def test_ingest_is_idempotent(conn):
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    ingest.ingest_papers(conn, FIXTURES / "papers.json")
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
