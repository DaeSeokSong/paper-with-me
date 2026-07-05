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


def test_ingest_evaluations_strips_parquet_null_unification(conn, tmp_path):
    """parquet 스키마 통합 버그 재현: 서로 다른 task의 지표 키가 하나의
    구조체로 합쳐지며 null로 채워진다. 적재 시 null 키가 제거되어야 한다."""
    records = [
        {"task": "Image Classification", "datasets": [
            {"dataset": "CIFAR-100", "sota": {"rows": [
                {"model_name": "EffNet-L2", "metrics": {"Accuracy": "96.08"},
                 "code_links": []}]}}], "subtasks": []},
        {"task": "Data-to-Text", "datasets": [
            {"dataset": "WebNLG", "sota": {"rows": [
                {"model_name": "T5", "metrics": {"Content Selection (F1)": "0.61"},
                 "code_links": []}]}}], "subtasks": []},
    ]
    shard_dir = _write_shards(records, tmp_path / "evals", n_shards=1)
    # pyarrow 통합으로 각 행의 metrics에 상대 task의 키가 null로 끼어드는지 확인
    raw = pq.read_table(shard_dir / "train-00000-of-00001.parquet").to_pylist()
    raw_metrics = raw[0]["datasets"][0]["sota"]["rows"][0]["metrics"]
    assert "Content Selection (F1)" in raw_metrics  # 오염 전제 성립

    assert ingest.ingest_evaluations(conn, shard_dir) == 2
    stored = json.loads(conn.execute(
        "SELECT metrics FROM sota_rows WHERE model_name='EffNet-L2'"
    ).fetchone()[0])
    assert stored == {"Accuracy": "96.08"}  # null 키 제거됨


def test_ingest_papers_parquet_date32_column(conn, tmp_path):
    """parquet 변환본의 date 컬럼은 문자열이 아니라 date32(datetime.date)로
    온다 — clean_date가 str만 받으면 papers.date 전체가 NULL로 적재되어
    Trends·Rising Tasks·최신순이 통째로 죽는다 (build-data 스모크 발견)."""
    import datetime

    records = json.loads((FIXTURES / "papers.json").read_text())
    table = pa.Table.from_pylist(records)
    idx = table.column_names.index("date")
    dates = pa.array(
        [datetime.date.fromisoformat(str(v)[:10]) for v in table.column("date").to_pylist()],
        type=pa.date32(),
    )
    table = table.set_column(idx, "date", dates)
    shard_dir = tmp_path / "papers"
    shard_dir.mkdir()
    pq.write_table(table, shard_dir / "train-00000-of-00001.parquet")

    assert ingest.ingest_papers(conn, shard_dir) == 2
    stored = conn.execute(
        "SELECT date FROM papers WHERE arxiv_id='1706.03762'"
    ).fetchone()[0]
    assert stored == "2017-06-12"


def test_ingest_links_from_parquet(conn, tmp_path):
    records = json.loads((FIXTURES / "links.json").read_text())
    shard_dir = _write_shards(records, tmp_path / "links", n_shards=1)
    assert ingest.ingest_links(conn, shard_dir) == 2


def test_incomplete_shards_raise(conn, tmp_path):
    """부분 다운로드된 샤드 디렉터리를 완전한 덤프로 착각하지 않는다."""
    records = json.loads((FIXTURES / "papers.json").read_text())
    shard_dir = _write_shards(records, tmp_path / "papers", n_shards=2)
    (shard_dir / "train-00001-of-00002.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="불완전"):
        ingest.ingest_papers(conn, shard_dir)
