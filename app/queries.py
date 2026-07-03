"""웹 앱 조회 레이어.

읽기 전용 SQLite 조회만 담당한다. 리스트형 컬럼(authors, tasks, metrics 등)은
DB에 JSON 텍스트로 저장되어 있으므로 여기서 파싱해 돌려준다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from pwc import db as pwc_db

PAGE_SIZE = 20


def connect(db_path: Path) -> sqlite3.Connection:
    conn = pwc_db.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _loads(row: dict, *keys: str) -> dict:
    out = dict(row)
    for k in keys:
        out[k] = json.loads(out[k]) if out.get(k) else []
    return out


def paper_slug(paper_url: str | None) -> str:
    return (paper_url or "").rstrip("/").rsplit("/", 1)[-1]


def _db_key(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


# ---------------------------------------------------------------- papers

def latest_papers(conn, page: int = 1) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM papers WHERE date IS NOT NULL ORDER BY date DESC "
        "LIMIT ? OFFSET ?", (PAGE_SIZE, (page - 1) * PAGE_SIZE)
    ).fetchall()
    return [_loads(r, "authors", "tasks", "methods") for r in rows]


def trending_papers(conn, limit: int = 10) -> list[dict]:
    """아카이브에는 실시간 스타 수가 없으므로 '구현체 수 × 최신성'을 근사치로 쓴다.

    전체 JOIN+GROUP BY(300k×576k)는 수십 초가 걸리므로, 날짜 인덱스로 최신
    논문 일부만 훑고 논문별 구현 수는 PK 프리픽스 조회로 센다.
    """
    rows = conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM repos r WHERE r.paper_url = p.paper_url)
                  AS repo_count
           FROM papers p WHERE p.date IS NOT NULL
           ORDER BY p.date DESC LIMIT 300"""
    ).fetchall()
    papers = [_loads(r, "authors", "tasks", "methods") for r in rows
              if r["repo_count"]]
    return papers[:limit]


def get_paper(conn, slug: str) -> dict | None:
    # 아카이브의 paper_url은 정규 형태라 PK 조회가 먼저 적중한다.
    # LIKE 폴백은 접두사가 다른 예외 레코드용 (576k 행 풀스캔이므로 최후 수단).
    row = conn.execute(
        "SELECT * FROM papers WHERE paper_url = ?",
        (f"https://paperswithcode.com/paper/{slug}",),
    ).fetchone() or conn.execute(
        "SELECT * FROM papers WHERE paper_url LIKE ? LIMIT 1", (f"%/paper/{slug}",)
    ).fetchone()
    return _loads(row, "authors", "tasks", "methods") if row else None


def paper_repos(conn, paper_url: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM repos WHERE paper_url = ? ORDER BY is_official DESC",
        (paper_url,),
    ).fetchall()
    return [dict(r) for r in rows]


def paper_results(conn, paper_url: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sota_rows WHERE paper_url = ? ORDER BY task, dataset",
        (paper_url,),
    ).fetchall()
    return [_loads(r, "metrics", "code_links") for r in rows]


def search_papers(conn, q: str, page: int = 1) -> list[dict]:
    offset = (page - 1) * PAGE_SIZE
    if pwc_db.has_fts(conn):
        try:
            rows = conn.execute(
                """SELECT p.* FROM papers_fts f
                   JOIN papers p ON p.rowid = f.rowid
                   WHERE papers_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?""",
                (_fts_query(q), PAGE_SIZE, offset),
            ).fetchall()
            return [_loads(r, "authors", "tasks", "methods") for r in rows]
        except sqlite3.OperationalError:
            pass  # 잘못된 FTS 구문은 LIKE로 폴백
    rows = conn.execute(
        "SELECT * FROM papers WHERE title LIKE ? ORDER BY date DESC LIMIT ? OFFSET ?",
        (f"%{q}%", PAGE_SIZE, offset),
    ).fetchall()
    return [_loads(r, "authors", "tasks", "methods") for r in rows]


def _fts_query(q: str) -> str:
    # 사용자 입력을 FTS 구문이 아닌 단순 단어 AND 매치로 취급한다
    words = re.findall(r"\w+", q)
    return " ".join(f'"{w}"' for w in words) if words else '""'


# ---------------------------------------------------------------- sota

def sota_tasks(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT task, COUNT(DISTINCT dataset) AS n_datasets, COUNT(*) AS n_rows,
                  MIN(parent_task) AS parent_task
           FROM sota_rows WHERE task IS NOT NULL
           GROUP BY task ORDER BY n_rows DESC"""
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["task"])} for r in rows]


_task_maps: dict[str, dict[str, str]] = {}


def find_task(conn, slug: str) -> str | None:
    """slug → task 이름. 목록은 DB 파일별로 1회만 만들어 캐시한다
    (아카이브는 불변 데이터)."""
    key = _db_key(conn)
    if key not in _task_maps:
        _task_maps[key] = {
            slugify(r["task"]): r["task"]
            for r in conn.execute(
                "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL"
            )
        }
    return _task_maps[key].get(slug)


def task_benchmarks(conn, task: str) -> list[dict]:
    """task의 벤치마크(dataset) 목록. 원본 사이트처럼 task 페이지에는
    카드 목록만 보여주고, 표는 dataset별 페이지에서 렌더링한다
    (대형 task는 dataset이 수천 개라 전체 표를 한 페이지에 담을 수 없다)."""
    rows = conn.execute(
        """SELECT dataset, COUNT(*) AS n_rows FROM sota_rows
           WHERE task = ? GROUP BY dataset ORDER BY n_rows DESC""",
        (task,),
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["dataset"] or "")} for r in rows]


def find_benchmark_dataset(conn, task: str, dataset_slug: str) -> str | None:
    for b in task_benchmarks(conn, task):
        if b["slug"] == dataset_slug:
            return b["dataset"]
    return None


def dataset_leaderboard(conn, task: str, dataset: str) -> dict:
    """단일 (task, dataset) 리더보드. 첫 번째 지표를 숫자로 파싱해
    내림차순 정렬하고, 파싱 불가 행은 뒤로 보낸다."""
    rows = [
        _loads(r, "metrics", "code_links")
        for r in conn.execute(
            "SELECT * FROM sota_rows WHERE task = ? AND dataset = ?",
            (task, dataset),
        )
    ]
    metric_names: list[str] = []
    for r in rows:
        if isinstance(r["metrics"], dict):
            for m in r["metrics"]:
                if m not in metric_names:
                    metric_names.append(m)
    # 지표 종류가 지나치게 많은 벤치마크는 상위(등장 빈도순 아님, 발견순) 일부만 컬럼으로
    metric_names = metric_names[:8]
    if metric_names:
        key = metric_names[0]
        rows.sort(
            key=lambda r: _metric_value(
                r["metrics"].get(key) if isinstance(r["metrics"], dict) else None
            ),
            reverse=True,
        )
    return {"dataset": dataset, "metric_names": metric_names, "rows": rows}


def _metric_value(raw: object) -> float:
    m = re.search(r"-?\d+(\.\d+)?", str(raw or ""))
    return float(m.group()) if m else float("-inf")


# ------------------------------------------------------- datasets/methods

def list_datasets(conn, q: str = "", page: int = 1) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM datasets WHERE name LIKE ? OR full_name LIKE ?
           ORDER BY num_papers DESC NULLS LAST LIMIT ? OFFSET ?""",
        (f"%{q}%", f"%{q}%", PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "modalities", "languages") for r in rows]


def get_dataset(conn, slug: str) -> dict | None:
    for r in conn.execute("SELECT * FROM datasets"):
        if slugify(r["name"]) == slug:
            return _loads(r, "modalities", "languages")
    return None


def dataset_leaderboards(conn, dataset_name: str) -> list[dict]:
    rows = conn.execute(
        """SELECT task, COUNT(*) AS n_rows FROM sota_rows
           WHERE dataset LIKE ? GROUP BY task ORDER BY n_rows DESC""",
        (f"%{dataset_name}%",),
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["task"])} for r in rows]


def list_methods(conn, q: str = "", page: int = 1) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM methods WHERE name LIKE ? OR full_name LIKE ?
           ORDER BY num_papers DESC NULLS LAST LIMIT ? OFFSET ?""",
        (f"%{q}%", f"%{q}%", PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "collections") for r in rows]


def get_method(conn, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM methods WHERE url LIKE ?", (f"%/method/{slug}",)
    ).fetchone()
    if row:
        return _loads(row, "collections")
    for r in conn.execute("SELECT * FROM methods"):
        if slugify(r["name"]) == slug:
            return _loads(r, "collections")
    return None


# ---------------------------------------------------------------- trends

def framework_trends(conn) -> dict:
    """연도별 프레임워크 구현체 점유율 (원본 PWC Trends 페이지 재현)."""
    rows = conn.execute(
        """SELECT substr(p.date, 1, 4) AS year, r.framework, COUNT(*) AS n
           FROM repos r JOIN papers p ON p.paper_url = r.paper_url
           WHERE p.date IS NOT NULL AND r.framework IS NOT NULL
                 AND r.framework NOT IN ('none', '')
           GROUP BY year, r.framework ORDER BY year"""
    ).fetchall()
    years = sorted({r["year"] for r in rows})
    frameworks = sorted({r["framework"] for r in rows})
    counts = {(r["year"], r["framework"]): r["n"] for r in rows}
    series = {}
    for fw in frameworks:
        total_by_year = {y: sum(counts.get((y, f), 0) for f in frameworks) for y in years}
        series[fw] = [
            round(100 * counts.get((y, fw), 0) / total_by_year[y], 1)
            if total_by_year[y] else 0.0
            for y in years
        ]
    return {"years": years, "series": series}


def stats(conn) -> dict:
    out = {}
    for table in ("papers", "repos", "datasets", "methods", "sota_rows"):
        out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    out["tasks"] = conn.execute(
        "SELECT COUNT(DISTINCT task) AS n FROM sota_rows"
    ).fetchone()["n"]
    return out
