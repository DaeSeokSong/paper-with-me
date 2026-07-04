"""덤프 JSON → SQLite 적재.

모든 덤프는 최상위가 JSON 배열이다(sota-extractor 포맷). 대용량 파일
(papers-with-abstracts는 수백 MB)을 고려해 ijson이 설치되어 있으면
스트리밍으로 파싱하고, 없으면 json.load로 폴백한다.
"""

from __future__ import annotations

import gzip
import json
import re
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
        shards = sorted(path.glob("*.parquet"))
        _check_shards_complete(shards)
        for shard in shards:
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


def _check_shards_complete(shards: list[Path]) -> None:
    """train-XXXXX-of-YYYYY 파일명 규약으로 샤드 완전성을 검증한다.

    다운로드가 중간에 끊긴 디렉터리를 완전한 덤프로 착각하고 부분 데이터로
    '성공' 적재하는 것을 막는다.
    """
    expected: set[int] | None = None
    have: set[int] = set()
    for shard in shards:
        m = re.search(r"-(\d+)-of-(\d+)\.parquet$", shard.name)
        if not m:
            return  # 규약을 따르지 않는 파일명이면 검증 생략
        have.add(int(m.group(1)))
        expected = {i for i in range(int(m.group(2)))}
    if expected is not None and have != expected:
        missing = sorted(expected - have)
        raise FileNotFoundError(
            f"parquet 샤드 불완전: {len(have)}/{len(expected)}개 "
            f"(누락 인덱스 {missing[:5]}...) — 다운로드를 다시 실행하세요"
        )


def _iter_parquet(path: Path) -> Iterator[dict]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    # evaluation-tables는 행 하나가 수 MB짜리 중첩 구조라 배치를 크게 잡으면
    # to_pylist()가 수 GB를 올려 OOM이 난다. 작은 배치로 스트리밍한다.
    for batch in pf.iter_batches(batch_size=64):
        yield from batch.to_pylist()


def strip_nulls(value: object) -> object:
    """중첩 구조에서 null 값을 재귀적으로 제거하고 bytes를 디코딩한다.

    parquet은 스키마가 균일해야 해서, HF 변환 과정에서 서로 다른 행의
    지표 키들이 하나의 구조체로 통합되고 해당 없는 자리는 null로 채워진다
    (예: 리더보드 행 하나가 전체 덤프의 지표 수천 종을 null로 갖게 됨).
    null 키를 걷어내야 원본 JSON 덤프와 같은 형태가 된다.
    """
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _nested(value: object) -> object:
    """중첩 필드 정규화.

    parquet 변환본에서는 중첩 구조가 네이티브 리스트/구조체로 오기도 하고,
    JSON 문자열로 직렬화되어 있기도 하다. 문자열이면 파싱을 시도하고,
    구조체 통합으로 생긴 null 채움은 제거한다.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return strip_nulls(value)


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
            if count % 200_000 == 0:
                print(f"  ... {count:,} rows", flush=True)
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    conn.commit()
    return count


def ingest_papers(conn: sqlite3.Connection, path: Path) -> int:
    # OR REPLACE는 DELETE+INSERT라 rowid가 바뀌어 외부 콘텐츠 FTS를
    # 오염시키고, 명시하지 않은 컬럼(source 등)을 DEFAULT로 리셋한다.
    # ON CONFLICT DO UPDATE는 rowid와 미명시 컬럼을 보존한다.
    sql = """INSERT INTO papers
             (paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
              proceeding, date, authors, tasks, methods)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(paper_url) DO UPDATE SET
               arxiv_id=excluded.arxiv_id, title=excluded.title,
               abstract=excluded.abstract, url_abs=excluded.url_abs,
               url_pdf=excluded.url_pdf, proceeding=excluded.proceeding,
               date=excluded.date, authors=excluded.authors,
               tasks=excluded.tasks, methods=excluded.methods"""
    rows = (
        (
            _paper_pk(r),
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
        if _paper_pk(r) is not None
    )
    return _executemany(conn, sql, rows)


def _paper_pk(record: dict) -> str | None:
    """papers PK 산출. paper_url이 없으면 제목 slug로 정규 URL을 만들어
    앱 라우팅과 일치시키고, 그마저 불가하면 None(스킵 대상)."""
    url = record.get("paper_url") or record.get("url_abs")
    if url:
        return url
    title = record.get("title")
    if not title:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"https://paperswithcode.com/paper/{slug}" if slug else None


def ingest_links(conn: sqlite3.Connection, path: Path) -> int:
    sql = """INSERT INTO repos
             (paper_url, repo_url, is_official, framework,
              mentioned_in_paper, mentioned_in_github)
             VALUES (?,?,?,?,?,?)
             ON CONFLICT(paper_url, repo_url) DO UPDATE SET
               is_official=excluded.is_official, framework=excluded.framework,
               mentioned_in_paper=excluded.mentioned_in_paper,
               mentioned_in_github=excluded.mentioned_in_github"""
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
              modalities, languages, num_papers, variants)
             VALUES (?,?,?,?,?,?,?,?,?,?)"""
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
            # 리더보드 dataset 문자열과 카탈로그명을 잇는 표기 변형
            _dumps(_nested(r.get("variants"))),
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
              paper_url, paper_title, paper_date, code_links, metrics_order,
              area, uses_additional_data)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""
    conn.execute("DELETE FROM sota_rows")

    def flatten(task_obj: dict, parent: str | None,
                area: str | None = None) -> Iterator[tuple]:
        task_name = task_obj.get("task")
        categories = _nested(task_obj.get("categories")) or []
        if categories and isinstance(categories[0], str):
            area = categories[0]  # 하위 task는 상위 분야를 상속
        for ds in _nested(task_obj.get("datasets")) or []:
            dataset_name = ds.get("dataset")
            sota = _nested(ds.get("sota")) or {}
            # 원본 지표 순서(첫 번째가 주 지표) — 리더보드 컬럼 선택에 사용
            metrics_order = _dumps(_nested(sota.get("metrics")))
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
                    metrics_order,
                    area,
                    # 원본 리더보드의 Extra Training Data 체크 컬럼
                    _to_int(row.get("uses_additional_data")),
                )
        for sub in _nested(task_obj.get("subtasks")) or []:
            yield from flatten(sub, task_name, area)

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
        print(f"[{name}] {path} 적재 중...", flush=True)
        counts[name] = INGESTERS[name](conn, path)
        print(f"  {counts[name]:,} rows", flush=True)
    db.rebuild_fts(conn)
    return counts


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    return int(bool(value))
