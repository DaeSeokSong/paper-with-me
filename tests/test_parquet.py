"""HF parquet 변환본 적재 테스트.

pwc-archive 저장소들은 원본 JSON이 아니라 parquet 샤드(data/train-*.parquet)로
보관되어 있다. 중첩 필드가 (1) 네이티브 구조체/리스트로 온 경우와
(2) JSON 문자열로 직렬화된 경우 모두 적재되어야 한다.
"""

import json
from pathlib import Path

import pytest

from pwc import db, ingest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "pwc.sqlite")
    yield conn
    conn.close()


def _write_shards(records: list[dict], shard_dir: Path, n_shards: int = 2) -> Path:
    shard_dir.mkdir(parents=True)
    for i in range(n_shards):
        chunk = records[i::n_shards]
        table = pa.Table.from_pylist(chunk)
        pq.write_table(table, shard_dir / f"train-{i:05d}-of-{n_shards:05d}.parquet")
    return shard_dir


def test_ingest_papers_from_parquet_native_nested(conn, tmp_path):
    records = json.loads((FIXTURES / "papers.json").read_text())
    shard_dir = _write_shards(records, tmp_path / "papers")
    assert ingest.ingest_papers(conn, shard_dir) == 2
    authors = conn.execute(
        "SELECT authors FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()[0]
    assert "Ashish Vaswani" in json.loads(authors)


def test_ingest_papers_from_parquet_json_string_nested(conn, tmp_path):
    records = json.loads((FIXTURES / "papers.json").read_text())
    for r in records:
        r["authors"] = json.dumps(r["authors"])
        r["methods"] = json.dumps(r["methods"])
    shard_dir = _write_shards(records, tmp_path / "papers")
    assert ingest.ingest_papers(conn, shard_dir) == 2
    authors = conn.execute(
        "SELECT authors FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()[0]
    assert json.loads(authors) == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]


def test_ingest_evaluations_from_parquet_json_string(conn, tmp_path):
    """재귀 구조(subtasks)는 parquet에서 JSON 문자열로 올 가능성이 높다."""
    records = json.loads((FIXTURES / "evaluation-tables.json").read_text())
    flat = [
        {
            "task": r["task"],
            "datasets": json.dumps(r["datasets"]),
            "subtasks": json.dumps(r["subtasks"]),
        }
        for r in records
    ]
    shard_dir = _write_shards(flat, tmp_path / "evaluations", n_shards=1)
    assert ingest.ingest_evaluations(conn, shard_dir) == 2
    sub = conn.execute(
        "SELECT parent_task FROM sota_rows WHERE model_name='ProtoNet'"
    ).fetchone()[0]
    assert sub == "Image Classification"


def test_ingest_links_from_parquet(conn, tmp_path):
    records = json.loads((FIXTURES / "links.json").read_text())
    shard_dir = _write_shards(records, tmp_path / "links", n_shards=1)
    assert ingest.ingest_links(conn, shard_dir) == 2
