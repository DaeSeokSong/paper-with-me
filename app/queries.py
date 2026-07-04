"""웹 앱 조회 레이어.

읽기 전용 SQLite 조회만 담당한다. 리스트형 컬럼(authors, tasks, metrics 등)은
DB에 JSON 텍스트로 저장되어 있으므로 여기서 파싱해 돌려준다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pwc import db as pwc_db
from pwc.ingest import strip_nulls

PAGE_SIZE = 20
# 리더보드 페이지당 행 수 — 기본 20, 사용자가 선택 가능한 값들
BOARD_PAGE_SIZE = 20
BOARD_PAGE_SIZES = (10, 20, 50, 100)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = pwc_db.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _loads(row: dict, *keys: str) -> dict:
    out = dict(row)
    for k in keys:
        # strip_nulls: parquet 스키마 통합으로 null이 채워진 기존 스냅샷도
        # 읽기 시점에 정화한다 (재빌드 전 배포본 대응)
        out[k] = strip_nulls(json.loads(out[k])) if out.get(k) else []
    return out


def paper_slug(paper_url: str | None) -> str:
    """논문 URL → /paper/{slug} 링크용 slug.

    canonical URL은 마지막 경로 조각이지만, 리더보드(sota_rows)는
    OpenReview처럼 쿼리스트링에 식별자가 있는 URL(forum?id=X)도 참조한다 —
    마지막 경로 조각을 그대로 쓰면 'forum?id=X' 같은 깨진 링크가 된다.
    """
    parts = urlsplit(paper_url or "")
    ids = parse_qs(parts.query).get("id")
    if ids and ids[0]:
        return ids[0]
    return parts.path.rstrip("/").rsplit("/", 1)[-1]


def _db_key(conn) -> tuple:
    """캐시 키: 경로 + 파일 mtime/크기 — 같은 경로에 스냅샷이 교체되는
    배포 환경(update-data 일일 갱신)에서도 캐시가 무효화되도록 한다."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        st = Path(path).stat()
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, 0, 0)


def _like(term: str) -> str:
    r"""LIKE 패턴용 이스케이프 — 사용자 입력의 %/_가 와일드카드로 해석되지
    않도록 한다. 반드시 ESCAPE '\' 절과 함께 사용."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- papers

_total_cache: dict[tuple, int] = {}


def total_papers(conn) -> int:
    """date가 있는 논문 총수 — /papers 페이저의 "n–m / 전체" 표시용.
    576k 행 COUNT는 요청마다 돌리기엔 아깝고 스냅샷 단위로 불변이라 캐시."""
    key = _db_key(conn)
    if key not in _total_cache:
        _total_cache.clear()
        _total_cache[key] = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE date IS NOT NULL"
        ).fetchone()[0]
    return _total_cache[key]


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
    # 신호(코드 링크·업보트·스타)가 있는 논문만 대상으로 최신 창을 자른다.
    # 무신호 신규 논문이 창을 채워 트렌딩이 통째로 비는 것을 방지 (창 절단
    # 전에 필터가 걸려야 한다).
    rows = conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM repos r WHERE r.paper_url = p.paper_url)
                  AS repo_count,
                  s.hf_upvotes, s.github_stars
           FROM papers p
           LEFT JOIN signals s ON s.paper_url = p.paper_url
           WHERE p.date IS NOT NULL
             AND ((s.paper_url IS NOT NULL
                   AND s.updated_at >= datetime('now', '-14 days'))
                  OR EXISTS (SELECT 1 FROM repos r WHERE r.paper_url = p.paper_url))
           ORDER BY p.date DESC LIMIT 300"""
    ).fetchall()
    papers = [_loads(r, "authors", "tasks", "methods") for r in rows]
    # 최신 창 안에서 인기 신호(업보트·스타·구현 수) 순으로 정렬
    papers.sort(key=lambda p: ((p.get("hf_upvotes") or 0) * 10
                               + (p.get("github_stars") or 0) / 10
                               + (p.get("repo_count") or 0)),
                reverse=True)
    return papers[:limit]


SLUG_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")

_paper_url_maps: dict[tuple, dict[str, str]] = {}


def _paper_url_map(conn) -> dict[str, str]:
    """비정형(비 canonical) 리더보드 paper_url의 slug → 원본 URL 맵.

    OpenReview/CVF/IEEE/Springer 등 canonical이 아닌 참조는 slug에서 원본
    URL을 역산할 수 없어 LIKE 접미 스캔이 필요했는데, 5GB 스냅샷 콜드
    캐시에서는 요청당 수십 초가 걸려 라이브 타임아웃을 냈다(배포 라이브
    점검에서 실측). 스냅샷 단위로 한 번만 훑어 맵으로 캐시하고, 요청
    경로는 인덱스 정확 일치만 쓴다.
    """
    key = _db_key(conn)
    if key not in _paper_url_maps:
        m: dict[str, str] = {}
        for (u,) in conn.execute(
            """SELECT DISTINCT paper_url FROM sota_rows
               WHERE paper_url IS NOT NULL
                 AND paper_url NOT LIKE 'https://paperswithcode.com/paper/%'"""
        ):
            s = paper_slug(u)
            if s:
                m.setdefault(s, u)
        _paper_url_maps.clear()  # 이전 스냅샷 엔트리 정리
        _paper_url_maps[key] = m
    return _paper_url_maps[key]


def get_paper(conn, slug: str) -> dict | None:
    # slug 형식 제한 — 와일드카드 주입과 무의미한 풀스캔을 차단한다.
    # 점(.)은 arXiv ID형 slug(예: 2010.01412) 때문에 허용한다.
    if not SLUG_RE.fullmatch(slug):
        return None
    # ① 정규 paper_url PK ② arXiv URL(리더보드가 arxiv.org 링크로 참조하는
    # 논문 — url_abs 인덱스, 버전 접미 vN은 떼고도 시도) ③ 비정형 URL 맵
    # (OpenReview forum?id= 등)으로 원본 URL을 찾아 인덱스 정확 일치.
    # LIKE 접미 스캔은 쓰지 않는다 — 5GB 스냅샷에서 요청당 수십 초.
    arxiv_ids = [slug]
    m = re.fullmatch(r"(\d{4}\.\d{4,5})v\d+", slug)
    if m:
        arxiv_ids.append(m.group(1))
    arxiv_urls = [f"{scheme}://arxiv.org/abs/{i}"
                  for i in arxiv_ids for scheme in ("https", "http")]
    row = conn.execute(
        "SELECT * FROM papers WHERE paper_url = ?",
        (f"https://paperswithcode.com/paper/{slug}",),
    ).fetchone() or conn.execute(
        "SELECT * FROM papers WHERE url_abs IN "
        f"({','.join('?' * len(arxiv_urls))}) LIMIT 1",
        arxiv_urls,
    ).fetchone()
    if row is None:
        src = _paper_url_map(conn).get(slug)
        if src:
            row = conn.execute(
                "SELECT * FROM papers WHERE paper_url = ?", (src,)
            ).fetchone() or conn.execute(
                "SELECT * FROM papers WHERE url_abs = ? LIMIT 1", (src,)
            ).fetchone()
    return _loads(row, "authors", "tasks", "methods") if row else None


def get_paper_stub(conn, slug: str) -> dict | None:
    """papers 덤프에 없지만 리더보드(sota_rows)가 참조하는 논문의 스텁.

    아카이브의 evaluation-tables는 papers-with-abstracts에 없는 논문도
    참조한다 — 리더보드에서 클릭한 논문이 404로 끊기지 않도록, 리더보드
    데이터로 초록 없는 전용 페이지를 구성한다.
    """
    if not SLUG_RE.fullmatch(slug):
        return None
    url = f"https://paperswithcode.com/paper/{slug}"
    rows = [
        _loads(r, "metrics", "code_links")
        for r in conn.execute(
            """SELECT * FROM sota_rows
               WHERE paper_url IN (?, ?, ?) ORDER BY id""",
            (url, f"https://arxiv.org/abs/{slug}",
             f"http://arxiv.org/abs/{slug}"),
        )
    ]
    if not rows:
        # 비정형 URL(OpenReview forum?id=, CVF, IEEE 등)은 slug → 원본 URL
        # 맵으로 역해석 후 인덱스 정확 일치 (LIKE 스캔 금지 — 위 주석 참조)
        src = _paper_url_map(conn).get(slug)
        if src:
            rows = [
                _loads(r, "metrics", "code_links")
                for r in conn.execute(
                    "SELECT * FROM sota_rows WHERE paper_url = ? ORDER BY id",
                    (src,),
                )
            ]
    if not rows:
        return None
    repos: dict[str, dict] = {}
    for r in rows:
        for c in r["code_links"]:
            link = c.get("url") if isinstance(c, dict) else (
                c if isinstance(c, str) else None)
            if link and link not in repos:
                repos[link] = {"repo_url": link, "is_official": None,
                               "framework": None, "stars": None}
    # 리더보드 행들의 task를 모아 일반 논문 페이지와 같은 탐색 허브를 만든다
    tasks = list(dict.fromkeys(r["task"] for r in rows if r.get("task")))
    return {
        "paper_url": url,
        "title": rows[0].get("paper_title") or slug.replace("-", " "),
        "date": rows[0].get("paper_date"),
        "abstract": None, "authors": [], "tasks": tasks, "methods": [],
        "arxiv_id": None, "url_abs": None, "url_pdf": None,
        "proceeding": None, "source": "archive", "stub": True,
        "repos": list(repos.values()), "results": rows,
    }


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
            if rows or page > 1:
                return [_loads(r, "authors", "tasks", "methods") for r in rows]
            # 단어 단위 FTS가 0건이면 부분 문자열 LIKE로 폴백 (제목 일부만
            # 아는 사용자, 이모지 등 토큰화 불가 문자 대응)
        except sqlite3.OperationalError:
            pass  # 잘못된 FTS 구문도 LIKE로 폴백
    rows = conn.execute(
        """SELECT * FROM papers WHERE title LIKE ? ESCAPE '\\'
           ORDER BY date DESC LIMIT ? OFFSET ?""",
        (f"%{_like(q)}%", PAGE_SIZE, offset),
    ).fetchall()
    return [_loads(r, "authors", "tasks", "methods") for r in rows]


def search_matches(conn, q: str, cap: int = 5) -> dict:
    """통합 검색 보조: 질의가 task/데이터셋/방법론 이름과 겹치면 함께 안내.

    캐시된 slug 맵만 훑으므로 스캔 비용이 없다 (검색이 논문 전용이라
    'imagenet' 검색이 0건으로 끝나던 사용성 문제 보완).
    """
    qs = slugify(q)
    if not qs:
        return {"tasks": [], "datasets": [], "methods": []}
    out: dict[str, list[dict]] = {}
    for kind, sql, prefix in (
        ("tasks", "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL",
         "/sota/"),
        ("datasets", "SELECT name FROM datasets", "/dataset/"),
        ("methods", "SELECT name FROM methods", "/method/"),
    ):
        m = _slug_map(conn, kind.rstrip("s"), sql)
        hits = [{"name": name, "url": prefix + slug}
                for slug, name in m.items() if qs in slug]
        # 짧은 slug(정확 일치에 가까운 것) 우선
        hits.sort(key=lambda h: len(h["name"]))
        out[kind] = hits[:cap]
    return out


def _fts_query(q: str) -> str:
    # 사용자 입력을 FTS 구문이 아닌 단순 단어 AND 매치로 취급한다.
    # 마지막 단어는 prefix 매치 — 타이핑 중 검색·한글 어절 앞부분 대응
    words = re.findall(r"\w+", q)
    if not words:
        return '""'
    phrases = [f'"{w}"' for w in words[:-1]]
    phrases.append(f'"{words[-1]}"*')
    return " ".join(phrases)


# ---------------------------------------------------------------- sota

def sota_tasks(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT task, COUNT(DISTINCT dataset) AS n_datasets, COUNT(*) AS n_rows,
                  COUNT(DISTINCT paper_url) AS n_papers,
                  MIN(parent_task) AS parent_task, MAX(area) AS area
           FROM sota_rows WHERE task IS NOT NULL
           GROUP BY task ORDER BY n_rows DESC"""
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["task"])} for r in rows]


def board_ranks(rows: list[dict], metric: str | None) -> list[int]:
    """리더보드 순위 — 주 지표가 같은 연속 행은 공동 순위(competition
    ranking). 행 순서는 원본 덤프 순서를 유지하고 번호만 계산한다
    (동점인데 1·2위로 갈려 금·은이 나뉘는 문제 방지)."""
    ranks: list[int] = []
    prev_val: float | None = None
    for i, r in enumerate(rows):
        val = None
        if metric and isinstance(r.get("metrics"), dict):
            try:
                val = float(str(r["metrics"].get(metric, "")
                                ).replace("%", "").replace(",", "").strip())
            except ValueError:
                val = None
        if ranks and val is not None and val == prev_val:
            ranks.append(ranks[-1])  # 공동 순위
        else:
            ranks.append(i + 1)
        prev_val = val
    return ranks


def sota_areas(conn) -> list[dict]:
    """원본 /sota 처럼 분야(area)별로 task를 그룹핑한다. area 정보가 없는
    구 스냅샷에서는 단일 그룹으로 폴백한다. 벤치마크가 0개인 task는
    카드가 죽은 길이 되므로 제외한다."""
    tasks = [t for t in sota_tasks(conn) if t["n_datasets"] > 0]
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        groups.setdefault(t["area"] or "Miscellaneous", []).append(t)
    if list(groups) == ["Miscellaneous"]:
        return [{"area": None, "tasks": tasks}]
    # 원본처럼 큰 분야(태스크 수 기준) 먼저
    return [
        {"area": area, "tasks": ts}
        for area, ts in sorted(groups.items(),
                               key=lambda kv: -len(kv[1]))
    ]


_slug_maps: dict[tuple, dict[str, str]] = {}


def _slug_map(conn, kind: str, sql: str) -> dict[str, str]:
    """slug → 이름 맵을 DB 스냅샷(경로+mtime) 단위로 캐시한다.
    스냅샷이 교체되면 키가 바뀌어 자동 무효화된다."""
    key = (_db_key(conn), kind)
    if key not in _slug_maps:
        # 오래된 스냅샷의 엔트리 정리 (일일 갱신으로 무한 증식 방지)
        for old in [k for k in _slug_maps if k[1] == kind and k != key]:
            del _slug_maps[old]
        _slug_maps[key] = {
            slugify(r[0]): r[0] for r in conn.execute(sql) if r[0]
        }
    return _slug_maps[key]


def find_task(conn, slug: str) -> str | None:
    return _slug_map(
        conn, "task",
        "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL",
    ).get(slug)


def task_benchmarks(conn, task: str) -> list[dict]:
    """task의 벤치마크(dataset) 목록. 원본 사이트처럼 task 페이지에는
    카드 목록만 보여주고, 표는 dataset별 페이지에서 렌더링한다
    (대형 task는 dataset이 수천 개라 전체 표를 한 페이지에 담을 수 없다)."""
    rows = conn.execute(
        """SELECT dataset, COUNT(*) AS n_rows FROM sota_rows
           WHERE task = ? AND dataset IS NOT NULL
           GROUP BY dataset ORDER BY n_rows DESC""",
        (task,),
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["dataset"])} for r in rows]


def find_benchmark_dataset(conn, task: str, dataset_slug: str) -> str | None:
    for b in task_benchmarks(conn, task):
        if b["slug"] == dataset_slug:
            return b["dataset"]
    return None


def dataset_leaderboard(conn, task: str, dataset: str) -> dict:
    """단일 (task, dataset) 리더보드.

    행 순서는 원본 덤프의 큐레이션 순서(rowid)를 그대로 보존한다 — 지표값
    기반 재정렬은 낮을수록 좋은 지표(Error rate 등)·표기 스케일 혼용
    ("95%" vs "0.95")·과학표기에서 순위를 왜곡하므로 하지 않는다.
    지표 컬럼은 원본 sota.metrics 순서(주 지표가 첫 번째)를 우선 사용하고,
    없는 구 스냅샷에서는 등장 빈도 상위로 폴백한다.
    """
    rows = []
    metric_names: list[str] = []
    for r in conn.execute(
        "SELECT * FROM sota_rows WHERE task = ? AND dataset = ? ORDER BY id",
        (task, dataset),
    ):
        row = _loads(r, "metrics", "code_links")
        if not metric_names and row.get("metrics_order"):
            order = strip_nulls(json.loads(row["metrics_order"]))
            if isinstance(order, list):
                metric_names = [m for m in order if isinstance(m, str)]
        rows.append(row)
    if not metric_names:
        counts = Counter(
            m for r in rows if isinstance(r["metrics"], dict) for m in r["metrics"]
        )
        metric_names = [m for m, _ in counts.most_common(8)]
    # 값이 하나도 없는 지표는 빈 컬럼만 만든다 — 표시 대상에서 제외
    metric_names = [
        m for m in metric_names
        if any(isinstance(r["metrics"], dict) and r["metrics"].get(m)
               for r in rows)
    ]
    metric_names = metric_names[:8]
    # 자동 추출(source='auto') 행은 id 순서상 맨 뒤에 붙으므로, 주 지표
    # 값 기준으로 원본 순서열 안의 제자리에 끼워 넣는다 (원본 행들의
    # 상대 순서는 그대로 유지 — 재정렬 금지 원칙은 원본 행에만 적용)
    primary = metric_names[0] if metric_names else None
    auto = [r for r in rows if r.get("source") == "auto"]
    if auto and primary:
        base = [r for r in rows if r.get("source") != "auto"]
        lower_better = bool(_LOWER_BETTER.search(primary))
        for a in auto:
            av = _metric_value(a, primary)
            pos = len(base)
            if av is not None:
                for i, b in enumerate(base):
                    bv = _metric_value(b, primary)
                    if bv is None:
                        continue
                    if (av > bv) if not lower_better else (av < bv):
                        pos = i
                        break
            base.insert(pos, a)
        rows = base
    return {"dataset": dataset, "metric_names": metric_names, "rows": rows}


def _metric_value(row: dict, metric: str) -> float | None:
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return None
    try:
        return float(str(metrics.get(metric, "")
                         ).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


_LOWER_BETTER = re.compile(
    r"error|loss|wer|cer|fid|mae|rmse|mse|perplexity|flops|params|latency|time",
    re.IGNORECASE,
)


def board_chart(rows: list[dict], metric: str | None) -> dict | None:
    """리더보드의 SOTA 추이 차트 데이터 (원본 PWC 리더보드 상단 차트 재현).

    주 지표를 숫자로 파싱해 (날짜, 값) 점들과 '현재까지 최고 기록' 곡선을
    만든다. 지표명이 낮을수록 좋은 계열(error/loss 등)이면 최소 기준.
    파싱 가능한 점이 3개 미만이면 차트를 생략한다(None).
    """
    if not metric:
        return None
    points = []
    for r in rows:
        raw = r["metrics"].get(metric) if isinstance(r["metrics"], dict) else None
        m = re.search(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", str(raw or ""))
        date = r.get("paper_date") or ""
        if m and re.match(r"\d{4}-\d{2}", date):
            points.append({"date": date, "value": float(m.group()),
                           "model": r.get("model_name") or ""})
    if len(points) < 3:
        return None
    points.sort(key=lambda p: p["date"])
    lower_better = bool(_LOWER_BETTER.search(metric))
    best = None
    frontier = []
    for p in points:
        if best is None or (p["value"] < best if lower_better
                            else p["value"] > best):
            best = p["value"]
            frontier.append(p)
    return {"metric": metric, "points": points, "frontier": frontier,
            "lower_better": lower_better}


# ------------------------------------------------------- datasets/methods

def list_datasets(conn, q: str = "", page: int = 1) -> list[dict]:
    pattern = f"%{_like(q)}%"
    rows = conn.execute(
        """SELECT * FROM datasets
           WHERE name LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\'
           ORDER BY num_papers DESC, url LIMIT ? OFFSET ?""",
        (pattern, pattern, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "modalities", "languages") for r in rows]


def get_dataset(conn, slug: str) -> dict | None:
    name = _slug_map(conn, "dataset", "SELECT name FROM datasets").get(slug)
    if name is None:
        return None
    row = conn.execute(
        "SELECT * FROM datasets WHERE name = ?", (name,)
    ).fetchone()
    return _loads(row, "modalities", "languages", "variants") if row else None


def dataset_leaderboards(conn, dataset_name: str,
                         variants: list | None = None) -> list[dict]:
    """데이터셋의 벤치마크 목록. 카탈로그명과 리더보드의 dataset 문자열이
    다른 경우(예: 'MS COCO' vs 'COCO test-dev')가 흔해 variants까지
    매칭한다 — 안 그러면 데이터셋 페이지가 조용히 빈 껍데기가 된다."""
    names = [dataset_name] + [v for v in (variants or [])
                              if isinstance(v, str) and v]
    rows = conn.execute(
        f"""SELECT task, dataset, COUNT(*) AS n_rows FROM sota_rows
            WHERE dataset IN ({','.join('?' * len(names))})
              AND task IS NOT NULL
            GROUP BY task, dataset ORDER BY n_rows DESC""",
        names,
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["task"]),
                       "dataset_slug": slugify(r["dataset"])} for r in rows]


def list_methods(conn, q: str = "", page: int = 1) -> list[dict]:
    pattern = f"%{_like(q)}%"
    rows = conn.execute(
        """SELECT * FROM methods
           WHERE name LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\'
           ORDER BY num_papers DESC, url LIMIT ? OFFSET ?""",
        (pattern, pattern, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "collections") for r in rows]


def get_method(conn, slug: str) -> dict | None:
    if not re.fullmatch(r"[a-z0-9-]{1,200}", slug):
        return None
    row = conn.execute(
        "SELECT * FROM methods WHERE url = ?",
        (f"https://paperswithcode.com/method/{slug}",),
    ).fetchone()
    if row:
        return _loads(row, "collections")
    name = _slug_map(conn, "method", "SELECT name FROM methods").get(slug)
    if name is None:
        return None
    row = conn.execute("SELECT * FROM methods WHERE name = ?", (name,)).fetchone()
    return _loads(row, "collections") if row else None


# ---------------------------------------------------------------- trends

_trends_cache: dict[tuple, dict] = {}


def framework_trends(conn) -> dict:
    """연도별 프레임워크 구현체 점유율 (원본 PWC Trends 페이지 재현).

    30만×57만 JOIN 집계라 요청당 1초 이상 걸리므로 스냅샷 단위로 캐시한다.
    """
    key = _db_key(conn)
    if key in _trends_cache:
        return _trends_cache[key]
    rows = conn.execute(
        """SELECT substr(p.date, 1, 4) AS year, lower(r.framework) AS fw,
                  COUNT(*) AS n
           FROM repos r JOIN papers p ON p.paper_url = r.paper_url
           WHERE p.date GLOB '[0-9][0-9][0-9][0-9]*'
                 AND r.framework IS NOT NULL
                 AND lower(r.framework) NOT IN ('none', '')
           GROUP BY year, fw ORDER BY year"""
    ).fetchall()
    years = sorted({r["year"] for r in rows})
    frameworks = sorted({r["fw"] for r in rows})
    counts = {(r["year"], r["fw"]): r["n"] for r in rows}
    total_by_year = {
        y: sum(counts.get((y, f), 0) for f in frameworks) for y in years
    }
    series = {
        fw: [
            round(100 * counts.get((y, fw), 0) / total_by_year[y], 1)
            if total_by_year[y] else 0.0
            for y in years
        ]
        for fw in frameworks
    }
    _trends_cache.clear()
    _trends_cache[key] = {"years": years, "series": series}
    return _trends_cache[key]


def stats(conn) -> dict:
    out = {}
    for table in ("papers", "repos", "datasets", "methods", "sota_rows"):
        out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    out["tasks"] = conn.execute(
        "SELECT COUNT(DISTINCT task) AS n FROM sota_rows"
    ).fetchone()["n"]
    return out
