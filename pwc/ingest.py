"""덤프 JSON → SQLite 적재.

모든 덤프는 최상위가 JSON 배열이다(sota-extractor 포맷). 대용량 파일
(papers-with-abstracts는 수백 MB)을 고려해 ijson이 설치되어 있으면
스트리밍으로 파싱하고, 없으면 json.load로 폴백한다.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import db

try:
    import ijson  # type: ignore
except ImportError:
    ijson = None

BATCH = 5000


def iter_records(path: Path) -> Iterator[dict]:
    """덤프의 레코드를 순회한다.

    - .json / .json.gz 파일: 최상위 배열 원소
    - 디렉터리 또는 .parquet 파일: parquet 샤드의 행 (HF 변환본)
    """
    if path.is_dir():
        for shard in sorted(path.glob("*.parquet")):
            yield from _iter_parquet(shard)
        return
    if path.suffix == ".parquet":
        yield from _iter_parquet(path)
        return
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as f:
        if ijson is not None:
            yield from ijson.items(f, "item")
        else:
            yield from json.load(f)


def _iter_parquet(path: Path) -> Iterator[dict]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=2000):
        yield from batch.to_pylist()


def _nested(value: object) -> object:
    """중첩 필드 정규화.

    parquet 변환본에서는 중첩 구조가 네이티브 리스트/구조체로 오기도 하고,
    JSON 문자열로 직렬화되어 있기도 하다. 문자열이면 파싱을 시도한다.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _dumps(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> int:
    """배치 단위로 INSERT하고 총 행 수를 반환한다."""
    count = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    conn.commit()
    return count


def ingest_papers(conn: sqlite3.Connection, path: Path) -> int:
    sql = """INSERT OR REPLACE INTO papers
             (paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
              proceeding, date, authors, tasks, methods)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    rows = (
        (
            r.get("paper_url") or r.get("url_abs") or r.get("title"),
            r.get("arxiv_id"),
            r.get("title"),
            r.get("abstract"),
            r.get("url_abs"),
            r.get("url_pdf"),
            r.get("proceeding"),
            r.get("date"),
            _dumps(_nested(r.get("authors"))),
            _dumps(_nested(r.get("tasks"))),
            _dumps(_nested(r.get("methods"))),
        )
        for r in iter_records(path)
    )
    return _executemany(conn, sql, rows)


def ingest_links(conn: sqlite3.Connection, path: Path) -> int:
    sql = """INSERT OR REPLACE INTO repos
             (paper_url, repo_url, is_official, framework,
              mentioned_in_paper, mentioned_in_github)
             VALUES (?,?,?,?,?,?)"""
    rows = (
        (
            r.get("paper_url"),
            r.get("repo_url"),
            _to_int(r.get("is_official")),
            r.get("framework"),
            _to_int(r.get("mentioned_in_paper")),
            _to_int(r.get("mentioned_in_github")),
        )
        for r in iter_records(path)
    )
    return _executemany(conn, sql, rows)


def ingest_datasets(conn: sqlite3.Connection, path: Path) -> int:
    sql = """INSERT OR REPLACE INTO datasets
             (url, name, full_name, homepage, description, paper_url,
              modalities, languages, num_papers)
             VALUES (?,?,?,?,?,?,?,?,?)"""
    rows = (
        (
            r.get("url") or r.get("name"),
            r.get("name"),
            r.get("full_name"),
            r.get("homepage"),
            r.get("description"),
            _paper_url_of(r),
            _dumps(_nested(r.get("modalities"))),
            _dumps(_nested(r.get("languages"))),
            r.get("num_papers"),
        )
        for r in iter_records(path)
    )
    return _executemany(conn, sql, rows)


def _paper_url_of(record: dict) -> str | None:
    paper = _nested(record.get("paper"))
    return paper.get("url") if isinstance(paper, dict) else None


def ingest_methods(conn: sqlite3.Connection, path: Path) -> int:
    sql = """INSERT OR REPLACE INTO methods
             (url, name, full_name, description, paper_url, introduced_year,
              source_url, source_title, num_papers, collections)
             VALUES (?,?,?,?,?,?,?,?,?,?)"""
    rows = (
        (
            r.get("url") or r.get("name"),
            r.get("name"),
            r.get("full_name"),
            r.get("description"),
            _paper_url_of(r),
            r.get("introduced_year"),
            r.get("source_url"),
            r.get("source_title"),
            r.get("num_papers"),
            _dumps(_nested(r.get("collections"))),
        )
        for r in iter_records(path)
    )
    return _executemany(conn, sql, rows)


def ingest_evaluations(conn: sqlite3.Connection, path: Path) -> int:
    """evaluation-tables의 중첩 구조(task → subtasks → datasets → sota.rows)를
    재귀적으로 평탄화하여 sota_rows에 적재한다."""
    sql = """INSERT INTO sota_rows
             (task, parent_task, dataset, model_name, metrics,
              paper_url, paper_title, paper_date, code_links)
             VALUES (?,?,?,?,?,?,?,?,?)"""
    conn.execute("DELETE FROM sota_rows")

    def flatten(task_obj: dict, parent: str | None) -> Iterator[tuple]:
        task_name = task_obj.get("task")
        for ds in _nested(task_obj.get("datasets")) or []:
            dataset_name = ds.get("dataset")
            sota = _nested(ds.get("sota")) or {}
            for row in _nested(sota.get("rows")) or []:
                yield (
                    task_name,
                    parent,
                    dataset_name,
                    row.get("model_name"),
                    _dumps(_nested(row.get("metrics"))),
                    row.get("paper_url"),
                    row.get("paper_title"),
                    row.get("paper_date"),
                    _dumps(_nested(row.get("code_links"))),
                )
        for sub in _nested(task_obj.get("subtasks")) or []:
            yield from flatten(sub, task_name)

    rows = (row for task in iter_records(path) for row in flatten(task, None))
    return _executemany(conn, sql, rows)


INGESTERS = {
    "papers": ingest_papers,
    "links": ingest_links,
    "datasets": ingest_datasets,
    "methods": ingest_methods,
    "evaluations": ingest_evaluations,
}


def ingest_all(conn: sqlite3.Connection, dumps: dict[str, Path]) -> dict[str, int]:
    """내려받은 덤프들을 순서대로 적재하고 이름 -> 행 수를 반환한다."""
    counts: dict[str, int] = {}
    for name, path in dumps.items():
        print(f"[{name}] {path} 적재 중...")
        counts[name] = INGESTERS[name](conn, path)
        print(f"  {counts[name]:,} rows")
    db.rebuild_fts(conn)
    return counts


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    return int(bool(value))
