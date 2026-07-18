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


def connect_fast(db_path: Path) -> sqlite3.Connection:
    """요청 경로용 — 스키마/마이그레이션 없이 연다 (기동 예열이 보장).
    요청마다 DDL+commit이 도는 쓰기 트랜잭션을 없애 수집 작업과의
    잠금 경쟁·지연을 제거한다."""
    conn = pwc_db.connect_fast(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _loads(row: dict, *keys: str) -> dict:
    # 아카이브 덤프에는 결측이 문자열 "None"으로 남은 컬럼이 있다(전수
    # 크롤에서 발견: kitti 보드의 model_name — NULL 가드와 `or` 폴백을
    # 모두 통과해 표에 'None'으로 노출). 읽기 시점에 일괄 정화해야
    # 이미 배포된 스냅샷도 안전하다.
    out = {k: (None if v == "None" else v) for k, v in dict(row).items()}
    for k in keys:
        # strip_nulls: parquet 스키마 통합으로 null이 채워진 기존 스냅샷도
        # 읽기 시점에 정화한다 (재빌드 전 배포본 대응)
        out[k] = strip_nulls(json.loads(out[k])) if out.get(k) else []
        # 덤프에 문자열 "None"이 지표 값으로 남은 행이 있다(전수 크롤에서
        # 발견: visual-place-recognition/kitti). 여기서 걷어내면 표·CSV·
        # 차트·API가 전부 정화된 값을 본다.
        if k == "metrics" and isinstance(out[k], dict):
            out[k] = {mk: mv for mk, mv in out[k].items()
                      if mv not in (None, "", "None", "none")}
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

# 아카이브에 '2222-12-22' 같은 오타 미래 날짜 행이 존재한다(적재 시
# clean_date로 정화하지만, 정화 전 스냅샷 대비 방어선) — 모든 최신성
# 질의에 상한을 건다
FUTURE_GUARD = "date <= date('now', '+1 day')"

_total_cache: dict[tuple, int] = {}


def total_papers(conn) -> int:
    """date가 있는 논문 총수 — /papers 페이저의 "n–m / 전체" 표시용.
    576k 행 COUNT는 요청마다 돌리기엔 아깝고 스냅샷 단위로 불변이라 캐시."""
    key = _db_key(conn)
    if key not in _total_cache:
        _total_cache.clear()
        _total_cache[key] = conn.execute(
            f"SELECT COUNT(*) FROM papers WHERE date IS NOT NULL "
            f"AND {FUTURE_GUARD}"
        ).fetchone()[0]
    return _total_cache[key]


def ensure_papers_tasks(conn) -> None:
    """태그(task) 기준 논문 검색용 역인덱스 papers_tasks를 구축한다.

    papers.tasks는 JSON 배열이라 태그 필터가 576k행 전체 스캔이 된다 —
    스냅샷당 한 번(meta 플래그로 멱등) 평탄화해 인덱스를 만든다.
    앱 기동 예열 시 호출되므로 요청 경로에는 비용이 없다."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS papers_tasks (
             task TEXT, paper_url TEXT)"""
    )
    # 플래그 존재 기반 멱등 — 재구축 트리거는 쓰기 주체(papers 재적재,
    # auto_tag, merge_live_data)가 플래그를 지우는 방식. 과거의 COUNT 비교는
    # 확인용 전체 스캔을 매 기동마다 유발했고, 개수 불변 내용 변경을 놓쳤다.
    flag = conn.execute(
        "SELECT value FROM meta WHERE key = 'papers_tasks_built'"
    ).fetchone()
    if flag:
        return
    conn.execute("DELETE FROM papers_tasks")
    try:
        conn.execute(
            """INSERT INTO papers_tasks (task, paper_url)
               SELECT DISTINCT je.value, p.paper_url
               FROM papers p, json_each(p.tasks) je
               WHERE p.tasks IS NOT NULL AND je.value IS NOT NULL"""
        )
    except sqlite3.OperationalError:
        # JSON1 미지원 환경 폴백 — 파이썬에서 평탄화
        batch = []
        for paper_url, tasks_json in conn.execute(
            "SELECT paper_url, tasks FROM papers WHERE tasks IS NOT NULL"
        ):
            try:
                tasks = json.loads(tasks_json)
            except ValueError:
                continue
            for t in dict.fromkeys(tasks if isinstance(tasks, list) else []):
                if isinstance(t, str) and t:
                    batch.append((t, paper_url))
        conn.executemany(
            "INSERT INTO papers_tasks (task, paper_url) VALUES (?, ?)", batch)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_tasks ON papers_tasks(task)")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) "
        "VALUES ('papers_tasks_built', '1')")
    conn.commit()


def papers_by_task(conn, task: str, page: int = 1) -> tuple[list[dict], int]:
    """태그(task명) 기준 논문 목록 + 총 건수. papers_tasks 인덱스 사용."""
    count_sql = f"""SELECT COUNT(*) FROM papers_tasks pt
                    JOIN papers p ON p.paper_url = pt.paper_url
                    WHERE pt.task = ? AND p.date IS NOT NULL
                      AND p.{FUTURE_GUARD}"""
    try:
        total = conn.execute(count_sql, (task,)).fetchone()[0]
    except sqlite3.OperationalError:  # 인덱스 미구축 (예열 전)
        ensure_papers_tasks(conn)
        total = conn.execute(count_sql, (task,)).fetchone()[0]
    rows = conn.execute(
        f"""SELECT p.* FROM papers_tasks pt
            JOIN papers p ON p.paper_url = pt.paper_url
            WHERE pt.task = ? AND p.date IS NOT NULL AND p.{FUTURE_GUARD}
            ORDER BY p.date DESC LIMIT ? OFFSET ?""",
        (task, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "authors", "tasks", "methods") for r in rows], total


def find_paper_task(conn, slug: str) -> str | None:
    """papers.tasks에 존재하는 task명을 slug로 찾는다 (리더보드에 없는
    태그의 목적지 — /papers?task= 로 안내)."""
    try:
        return _slug_map(
            conn, "papertask",
            "SELECT DISTINCT task FROM papers_tasks",
        ).get(slug)
    except sqlite3.OperationalError:  # 인덱스 미구축 (예열 전)
        return None


def suggest(conn, q: str, cap: int = 10) -> list[dict]:
    """검색 자동완성 — 카탈로그(태스크·데이터셋·방법론)는 캐시 맵 prefix
    일치로 즉답, 논문 제목은 FTS로 상위 몇 건."""
    q = (q or "").strip()
    qs = slugify(q)
    out: list[dict] = []
    if not qs:
        return out
    for kind, sql, prefix, label in (
        ("task", "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL",
         "/sota/", "벤치마크"),
        ("dataset", "SELECT name FROM datasets", "/dataset/", "데이터셋"),
        ("method", "SELECT name FROM methods", "/method/", "방법론"),
    ):
        m = _slug_map(conn, kind, sql)
        hits = sorted((slug_ for slug_ in m if slug_.startswith(qs)),
                      key=len)[:3]
        out += [{"label": m[h], "url": prefix + h, "kind": label}
                for h in hits]
    if len(q) >= 3 and pwc_db.has_fts(conn):
        try:
            rows = conn.execute(
                """SELECT p.title, p.paper_url FROM papers_fts f
                   JOIN papers p ON p.rowid = f.rowid
                   WHERE papers_fts MATCH ? ORDER BY rank LIMIT 5""",
                (_fts_query(q),),
            ).fetchall()
            out += [{"label": r["title"],
                     "url": "/paper/" + paper_slug(r["paper_url"]),
                     "kind": "논문"}
                    for r in rows if r["title"]]
        except sqlite3.OperationalError:
            pass
    return out[:cap]


def latest_papers(conn, page: int = 1) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM papers WHERE date IS NOT NULL AND {FUTURE_GUARD} "
        "ORDER BY date DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, (page - 1) * PAGE_SIZE)
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
             AND p.date <= date('now', '+1 day')
             AND ((s.paper_url IS NOT NULL
                   AND s.updated_at >= datetime('now', '-14 days'))
                  OR EXISTS (SELECT 1 FROM repos r WHERE r.paper_url = p.paper_url))
           ORDER BY p.date DESC LIMIT 300"""
    ).fetchall()
    papers = [_loads(r, "authors", "tasks", "methods") for r in rows]
    # 최신 창 안에서 인기 신호(업보트·스타·구현 수) 순으로 정렬
    papers.sort(key=_popularity, reverse=True)
    return papers[:limit]


def _popularity(p: dict) -> float:
    """인기 점수 — 트렌딩과 다이제스트가 같은 기준을 쓰도록 공유한다."""
    return ((p.get("hf_upvotes") or 0) * 10
            + (p.get("github_stars") or 0) / 10
            + (p.get("repo_count") or 0))


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
    # get_paper와 동일하게 버전 접미(vN)를 뗀 arXiv URL도 시도 — 한쪽만
    # 지원하면 같은 slug가 완전 페이지에선 열리고 스텁에선 404가 된다
    arxiv_ids = [slug]
    m = re.fullmatch(r"(\d{4}\.\d{4,5})v\d+", slug)
    if m:
        arxiv_ids.append(m.group(1))
    candidates = [url] + [f"{scheme}://arxiv.org/abs/{i}"
                          for i in arxiv_ids for scheme in ("https", "http")]
    rows = [
        _loads(r, "metrics", "code_links")
        for r in conn.execute(
            f"""SELECT * FROM sota_rows
                WHERE paper_url IN ({','.join('?' * len(candidates))})
                ORDER BY id""",
            candidates,
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


def paper_result_ranks(conn, results: list[dict],
                       max_boards: int = 8) -> list[int | None]:
    """논문의 각 벤치마크 결과가 보드에서 몇 위인지 — 원본 논문 페이지의
    'Ranked #N' 배지 재현. 보드 로드 비용이 있어 앞쪽 일부만 계산한다."""
    ranks: list[int | None] = []
    for r in results:
        if len(ranks) >= max_boards or not (r.get("task") and r.get("dataset")):
            ranks.append(None)
            continue
        board = dataset_leaderboard(conn, r["task"], r["dataset"])
        primary = board["metric_names"][0] if board["metric_names"] else None
        board_rank = board_ranks(board["rows"], primary)
        rank = next((rk for row, rk in zip(board["rows"], board_rank)
                     if row.get("id") == r.get("id")), None)
        ranks.append(rank)
    return ranks


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
            if rows:
                return [_loads(r, "authors", "tasks", "methods") for r in rows]
            # FTS가 0건이면 부분 문자열 LIKE로 폴백 (제목 일부만 아는
            # 사용자, 이모지 등 토큰화 불가 문자 대응). page>1에서도
            # 폴백해야 LIKE 결과의 2페이지 이후가 비지 않는다.
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


def task_slugs(conn) -> set[str]:
    """리더보드가 존재하는 task slug 집합 — 논문 카드의 태스크 태그를
    링크로 만들지 라벨로 둘지 판단용 (죽은 링크 방지, 캐시됨)."""
    return set(_slug_map(
        conn, "task",
        "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL"))


# 제목 유사도 질의에서 변별력이 없는 상투어
_TITLE_STOP = {
    "with", "from", "that", "this", "using", "based", "toward", "towards",
    "learning", "neural", "network", "networks", "deep", "model", "models",
    "improving", "efficient", "robust", "novel", "approach", "method",
}


def similar_papers(conn, paper: dict, limit: int = 5) -> list[dict]:
    """제목 키워드 FTS로 유사 논문을 찾는다 (Connected Papers류 수요 대응).

    외부 API 없이 기존 FTS 인덱스만 사용 — 제목의 변별력 있는 단어들을
    OR 매치해 bm25 순으로 돌려준다."""
    if not pwc_db.has_fts(conn):
        return []
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}",
                                           paper.get("title") or "")
             if w.lower() not in _TITLE_STOP][:6]
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)
    try:
        rows = conn.execute(
            """SELECT p.* FROM papers_fts f JOIN papers p ON p.rowid = f.rowid
               WHERE papers_fts MATCH ? AND p.paper_url != ?
               ORDER BY rank LIMIT ?""",
            (match, paper.get("paper_url") or "", limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_loads(r, "authors", "tasks", "methods") for r in rows]


def methods_glossary(conn, method_names: list) -> list[dict]:
    """논문에 쓰인 방법론의 이름·설명 요약 — 용어가 이해를 막는 문제 대응."""
    names = []
    for m in method_names or []:
        name = m.get("name") if isinstance(m, dict) else m
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    names = names[:8]
    if not names:
        return []
    rows = conn.execute(
        f"""SELECT name, description FROM methods
            WHERE name IN ({','.join('?' * len(names))})""",
        names,
    ).fetchall()
    by_name = {r["name"]: r["description"] for r in rows}
    out = []
    for name in names:
        desc = (by_name.get(name) or "").strip()
        if len(desc) > 180:
            desc = desc[:180].rsplit(" ", 1)[0] + "…"
        out.append({"name": name, "slug": slugify(name), "description": desc,
                    "in_catalog": name in by_name})
    return out


_area_maps: dict[tuple, dict] = {}


def _area_map(conn) -> dict:
    """task → area 맵 — 스냅샷 단위 불변인데 주차 캐시 미스마다 155k행
    GROUP BY를 재실행하던 것을 캐시로 대체."""
    key = _db_key(conn)
    if key not in _area_maps:
        _area_maps.clear()
        _area_maps[key] = dict(conn.execute(
            "SELECT task, MAX(area) FROM sota_rows "
            "WHERE task IS NOT NULL AND area IS NOT NULL GROUP BY task"))
    return _area_maps[key]


_digest_cache: dict[tuple, dict] = {}


def _digest_anchor(conn) -> str | None:
    """다이제스트 기준일 — 미래 오타 날짜를 제외한 가장 최근 논문 날짜."""
    row = conn.execute(
        "SELECT MAX(date) FROM papers WHERE date <= date('now', '+1 day')"
    ).fetchone()
    return row[0] if row and row[0] else None


def weekly_digest(conn, year: int | None = None,
                  week: int | None = None) -> dict:
    """ISO 주차 단위 다이제스트 (HF Daily Papers류 수요) + 아카이브 탐색.

    year/week가 없으면 가장 최근 논문이 속한 주. 이전/다음 주로 이동할
    수 있어 과거 다이제스트도 볼 수 있다. 분야는 task→area 맵
    (sota_rows)으로 정하고, 매핑이 없으면 'General'."""
    import datetime as _dt
    anchor_s = _digest_anchor(conn)
    anchor = (_dt.date.fromisoformat(anchor_s[:10])
              if anchor_s else _dt.date.today())
    anchor_iso = anchor.isocalendar()
    if not (year and week):
        year, week = anchor_iso[0], anchor_iso[1]
    try:
        start = _dt.date.fromisocalendar(year, week, 1)
    except ValueError:
        year, week = anchor_iso[0], anchor_iso[1]
        start = _dt.date.fromisocalendar(year, week, 1)
    end = start + _dt.timedelta(days=6)
    key = (_db_key(conn), year, week)
    if key in _digest_cache:
        return _digest_cache[key]
    rows = conn.execute(
        """SELECT p.*, s.hf_upvotes, s.github_stars,
                  (SELECT COUNT(*) FROM repos r WHERE r.paper_url = p.paper_url)
                  AS repo_count
           FROM papers p LEFT JOIN signals s ON s.paper_url = p.paper_url
           WHERE p.date >= ? AND p.date <= ?""",
        (start.isoformat(), end.isoformat() + "~"),  # '~' > '9': 시각 접미 포함
    ).fetchall()
    papers = [_loads(r, "authors", "tasks", "methods") for r in rows]
    papers.sort(key=_popularity, reverse=True)
    area_of = _area_map(conn)
    groups: dict[str, list[dict]] = {}
    for p in papers:
        area = next((area_of[t] for t in p["tasks"] if t in area_of), None)
        groups.setdefault(area or "General", []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    prev_iso = (start - _dt.timedelta(days=7)).isocalendar()
    next_start = start + _dt.timedelta(days=7)
    has_next = next_start <= anchor
    out = {"year": year, "week": week,
           "start": start.isoformat(), "end": end.isoformat(),
           "total": len(papers),
           "prev": {"year": prev_iso[0], "week": prev_iso[1]},
           "next": ({"year": next_start.isocalendar()[0],
                     "week": next_start.isocalendar()[1]}
                    if has_next else None),
           "groups": [{"area": a, "papers": ps[:8], "total": len(ps)}
                      for a, ps in ordered]}
    # 이전 스냅샷 엔트리 우선 제거; 그래도 넘치면 오래된 것부터 —
    # 전체 clear는 현재 주차 핫 캐시까지 증발시킨다
    for stale in [k for k in _digest_cache if k[0] != key[0]]:
        del _digest_cache[stale]
    while len(_digest_cache) > 60:
        del _digest_cache[next(iter(_digest_cache))]
    _digest_cache[key] = out
    return out


_rising_cache: dict[tuple, list] = {}


def rising_tasks(conn, top: int = 15) -> list[dict]:
    """월별 논문 수 기준 급상승 task + 스파크라인 데이터.

    아카이브가 특정 시점에 동결될 수 있으므로 '지금'이 아니라 데이터의
    마지막 월을 앵커로 최근 8개월을 본다. json_each(JSON1) 미지원
    환경에서는 빈 목록 (섹션 숨김)."""
    key = _db_key(conn)
    if key in _rising_cache:
        return _rising_cache[key]
    try:
        rows = conn.execute(
            """SELECT je.value AS task, substr(p.date, 1, 7) AS month,
                      COUNT(*) AS n
               FROM papers p, json_each(p.tasks) je
               WHERE p.date IS NOT NULL AND p.tasks IS NOT NULL
                 AND p.date <= date('now', '+1 day')
                 AND p.date >= date((SELECT MAX(date) FROM papers
                                     WHERE date <= date('now', '+1 day')),
                                    '-9 months')
                 AND je.value IS NOT NULL
               GROUP BY task, month"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    months = sorted({r["month"] for r in rows})[-8:]
    if len(months) < 4:
        _rising_cache.clear()
        _rising_cache[key] = []
        return []
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["month"] in months:
            counts.setdefault(r["task"], {})[r["month"]] = r["n"]
    scored = []
    half = len(months) // 2
    for task, by_month in counts.items():
        series = [by_month.get(m, 0) for m in months]
        recent, past = sum(series[half:]), sum(series[:half])
        if recent < 5:
            continue  # 표본이 너무 작은 태스크 제외
        growth = (recent - past) / (past or 1)
        scored.append({"task": task, "slug": slugify(task), "series": series,
                       "recent": recent, "growth": round(growth, 2)})
    scored.sort(key=lambda t: (-t["growth"], -t["recent"]))
    out = scored[:top]
    _rising_cache.clear()
    _rising_cache[key] = out
    if out:
        out_months = months
        for t in out:
            t["months"] = out_months
    return out


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
        val = _metric_value(r, metric) if metric else None
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


_task_variant_maps: dict[tuple, dict[str, list[str]]] = {}


def task_variants(conn, slug: str) -> list[str]:
    """같은 slug로 합쳐지는 task 문자열 변형 전부.

    아카이브에는 'Class Incremental Learning'과 'class-incremental
    learning'처럼 대소문자·구두점만 다른 task가 공존한다 — 대표 하나만
    쓰면 변형 쪽에만 있는 벤치마크가 404가 된다 (전수 크롤에서 발견)."""
    key = _db_key(conn)
    if key not in _task_variant_maps:
        m: dict[str, list[str]] = {}
        for (t,) in conn.execute(
            "SELECT DISTINCT task FROM sota_rows WHERE task IS NOT NULL"
        ):
            m.setdefault(slugify(t), []).append(t)
        _task_variant_maps.clear()
        _task_variant_maps[key] = m
    return _task_variant_maps[key].get(slug, [])


def task_benchmarks(conn, task: str,
                    variants: list | None = None) -> list[dict]:
    """task의 벤치마크(dataset) 목록. 원본 사이트처럼 task 페이지에는
    카드 목록만 보여주고, 표는 dataset별 페이지에서 렌더링한다
    (대형 task는 dataset이 수천 개라 전체 표를 한 페이지에 담을 수 없다).
    variants가 있으면 같은 slug의 task 표기 변형을 병합한다."""
    names = list(dict.fromkeys([task] + (variants or [])))
    rows = conn.execute(
        f"""SELECT dataset, task, COUNT(*) AS n_rows FROM sota_rows
            WHERE task IN ({','.join('?' * len(names))})
              AND dataset IS NOT NULL
            GROUP BY dataset, task ORDER BY n_rows DESC""",
        names,
    ).fetchall()
    return [dict(r) | {"slug": slugify(r["dataset"])} for r in rows]


def resolve_board(conn, task_slug: str,
                  dataset_slug: str) -> tuple[str | None, str | None]:
    """(task_slug, dataset_slug) → (task명, dataset명). 표기 변형까지 시도.

    HTML 라우트와 API가 같은 해석 규칙을 쓰도록 공유한다 — 한쪽에만
    변형 폴백이 있으면 같은 보드가 웹에선 열리고 API에선 404가 된다."""
    task = find_task(conn, task_slug)
    if not task:
        return None, None
    dataset = find_benchmark_dataset(conn, task, dataset_slug)
    if dataset is None:
        for alt in task_variants(conn, task_slug):
            if alt == task:
                continue
            dataset = find_benchmark_dataset(conn, alt, dataset_slug)
            if dataset is not None:
                return alt, dataset
    return task, dataset


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
        row = _loads(r, "metrics", "code_links", "tags")
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
    # 값이 하나도 없는 지표는 빈 컬럼만 만든다 — 표시 대상에서 제외.
    # truthiness가 아니라 존재 검사여야 한다: 값 0/0.0(Error=0 등)은 유효.
    metric_names = [
        m for m in metric_names
        if any(isinstance(r["metrics"], dict)
               and r["metrics"].get(m) not in (None, "")
               for r in rows)
    ]
    metric_names = metric_names[:8]
    # 자동 추출(auto)·외부 미러(external) 행은 id 순서상 맨 뒤에 붙으므로,
    # 주 지표 값 기준으로 원본 순서열 안의 제자리에 끼워 넣는다 (원본
    # 행들의 상대 순서는 그대로 유지 — 재정렬 금지 원칙은 원본 행에만)
    primary = metric_names[0] if metric_names else None
    _merged_sources = ("auto", "external")
    auto = [r for r in rows if r.get("source") in _merged_sources]
    if auto and primary:
        base = [r for r in rows if r.get("source") not in _merged_sources]
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

def _dataset_filter_sql(modality: str, language: str) -> tuple[str, list]:
    """모달리티/언어 필터 조건 — 원본 /datasets 좌측 필터 패널 재현."""
    sql, params = "", []
    for col, val in (("modalities", modality), ("languages", language)):
        if val:
            sql += (f" AND d.{col} IS NOT NULL AND json_valid(d.{col})"
                    f" AND EXISTS (SELECT 1 FROM json_each(d.{col}) je"
                    f"             WHERE je.value = ?)")
            params.append(val)
    return sql, params


def list_datasets(conn, q: str = "", page: int = 1,
                  modality: str = "", language: str = "") -> list[dict]:
    pattern = f"%{_like(q)}%"
    extra, extra_params = _dataset_filter_sql(modality, language)
    rows = conn.execute(
        f"""SELECT * FROM datasets d
           WHERE (name LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\')
           {extra}
           ORDER BY num_papers DESC, url LIMIT ? OFFSET ?""",
        (pattern, pattern, *extra_params,
         PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "modalities", "languages") for r in rows]


_facets_cache: dict[tuple, dict] = {}


def dataset_facets(conn, q: str = "") -> dict[str, list[dict]]:
    """모달리티/언어별 데이터셋 수 — 필터 패널의 항목·건수.
    검색어가 없는 기본 화면이 대부분이므로 스냅샷 단위로 캐시한다."""
    key = (_db_key(conn), q)
    if key in _facets_cache:
        return _facets_cache[key]
    pattern = f"%{_like(q)}%"
    out: dict[str, list[dict]] = {}
    for name, col in (("modalities", "modalities"), ("languages", "languages")):
        rows = conn.execute(
            f"""SELECT je.value AS value, COUNT(*) AS n
               FROM datasets d, json_each(d.{col}) je
               WHERE (d.name LIKE ? ESCAPE '\\' OR d.full_name LIKE ? ESCAPE '\\')
                 AND d.{col} IS NOT NULL AND json_valid(d.{col})
                 AND je.value IS NOT NULL
               GROUP BY je.value ORDER BY n DESC, je.value LIMIT 14""",
            (pattern, pattern),
        ).fetchall()
        out[name] = [dict(r) for r in rows]
    _facets_cache.clear()
    _facets_cache[key] = out
    return out


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


def list_methods(conn, q: str = "", page: int = 1,
                 collection: str = "") -> list[dict]:
    pattern = f"%{_like(q)}%"
    extra, params = "", [pattern, pattern]
    if collection:
        # 컬렉션(예: 'Convolutional Neural Networks')으로 필터 — 원본
        # Methods 인덱스의 카테고리 탐색 재현
        extra = (" AND collections IS NOT NULL AND json_valid(collections)"
                 " AND EXISTS (SELECT 1 FROM json_each(methods.collections) je"
                 "             WHERE json_extract(je.value, '$.collection') = ?)")
        params.append(collection)
    rows = conn.execute(
        f"""SELECT * FROM methods
           WHERE (name LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\')
           {extra}
           ORDER BY num_papers DESC, url LIMIT ? OFFSET ?""",
        (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return [_loads(r, "collections") for r in rows]


_collections_cache: dict[tuple, list[dict]] = {}


def method_collections(conn, limit: int = 24) -> list[dict]:
    """방법론 컬렉션(카테고리)별 건수 — 원본 Methods 인덱스 카테고리 카드."""
    key = (_db_key(conn), limit)
    if key in _collections_cache:
        return _collections_cache[key]
    rows = conn.execute(
        """SELECT json_extract(je.value, '$.collection') AS name,
                  COUNT(*) AS n
           FROM methods m, json_each(m.collections) je
           WHERE m.collections IS NOT NULL AND json_valid(m.collections)
             AND json_extract(je.value, '$.collection') IS NOT NULL
           GROUP BY name ORDER BY n DESC, name LIMIT ?""",
        (limit,),
    ).fetchall()
    _collections_cache.clear()
    _collections_cache[key] = [dict(r) for r in rows]
    return _collections_cache[key]


def most_implemented(conn, task: str, limit: int = 6) -> list[dict]:
    """task 논문 중 구현 저장소가 가장 많은 논문 — 원본 task 페이지의
    'Most implemented papers' 섹션 재현."""
    ensure_papers_tasks(conn)
    rows = conn.execute(
        """SELECT p.*, COUNT(r.repo_url) AS n_repos
           FROM papers_tasks pt
           JOIN papers p ON p.paper_url = pt.paper_url
           JOIN repos r ON r.paper_url = p.paper_url
           WHERE pt.task = ?
           GROUP BY p.paper_url
           ORDER BY n_repos DESC, p.date DESC LIMIT ?""",
        (task, limit),
    ).fetchall()
    return [_loads(r, "authors", "tasks", "methods") for r in rows]


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


# ------------------------------------------------------- AI 모델 비교

# /models 페이지 — Artificial Analysis 원본 4개 차트만 미러 (사용자 요청:
# 전체 AA는 과하고, 핵심 4개 지표만)
# Math Index는 사용자 요청으로 /agents에서 제외 (수집·/sota 보드는 유지).
# order: 표 정렬 방향 — 가격은 '비싼 순'(사용자 요청: 오름차순이면 무명
# 저가 모델만 상위에 노출되고 프런티어 모델이 안 보인다).
# desc: 지표가 무엇을 어떻게 측정하는지 2~3줄 요약 (사용자 요청).
MODEL_BOARDS = [
    {"dataset": "Artificial Analysis Intelligence Index", "metric": "Index",
     "order": "desc", "badge": None,
     "desc": "MMLU-Pro·GPQA Diamond·HLE·LiveCodeBench·SciCode·AA-LCR·"
             "Terminal-Bench 등 10개 안팎의 평가를 Artificial Analysis가 "
             "가중 합성한 종합 지능 지수(0~100). 높을수록 전반적 성능이 "
             "좋습니다."},
    {"dataset": "Artificial Analysis Coding Index", "metric": "Index",
     "order": "desc", "badge": None,
     "desc": "LiveCodeBench·SciCode·Terminal-Bench 등 코딩 관련 평가만 "
             "합성한 지수(0~100). 코드 생성과 터미널 작업 수행 능력을 "
             "봅니다."},
    {"dataset": "Humanity's Last Exam", "metric": "Accuracy",
     "order": "desc", "badge": None,
     "desc": "전 분야 전문가들이 출제한 최고 난도 시험(2,500+ 문항)의 "
             "정답률(%). 프런티어 모델도 수십 % 수준에 머무는 미포화 "
             "벤치마크입니다."},
    {"dataset": "AA-Omniscience Hallucination Rate",
     "metric": "Hallucination Rate", "order": "asc", "badge": "낮을수록 좋음",
     "desc": "AA-Omniscience 지식 평가에서 모른다고 답해야 할 문항에 "
             "그럴싸한 오답을 지어낸 비율(%). 낮을수록 답변을 신뢰할 수 "
             "있습니다."},
    {"dataset": "Cost per Intelligence Index Task",
     "metric": "Cost per task (USD)", "order": "asc",
     "badge": "낮을수록 좋음",
     "desc": "Intelligence Index 평가 태스크 1건을 수행하는 데 드는 평균 "
             "비용(USD). 토큰 단가와 응답 길이를 함께 반영합니다."},
    {"dataset": "Price per 1M Tokens (Blended 3:1)",
     "metric": "USD per 1M Tokens", "order": "desc",
     "badge": "지능 상위 25 · 비싼 순",
     # 전체 576개 중 가격순 상위/하위는 구형 고가 모델·무명 저가 모델만
     # 노출된다(사용자 피드백: Fable·Sol이 안 보임) — 지능 지수 상위
     # 25개 모델로 대상을 좁혀 '지금 쓸 만한 모델의 가격'만 보여준다.
     # 기본은 비싼 순(사용자 요청), 정렬 버튼으로 전환 가능.
     "top_by": ("Artificial Analysis Intelligence Index", "Index"),
     "desc": "입력:출력 3:1 혼합 기준 1백만 토큰당 API 가격(USD). "
             "Intelligence Index 상위 25개 프런티어 모델만 대상으로, "
             "같은 급 안에서 가격을 비교합니다 (기본 비싼 순)."},
]

# 모델명 → 논문 링크는 스냅샷이 갈리기 전까지 불변이므로 프로세스
# 수명 동안 캐시한다 (새 스냅샷 배포 = 프로세스 재시작)
_agent_paper_cache: dict[str, str | None] = {}


def _agent_paper(conn, model_name: str) -> str | None:
    """모델명 → 우리 DB의 테크 리포트/논문 페이지 링크.

    AA는 수치만 보여주지만 우리는 논문 DB가 있다 — 수치에서 논문·코드로
    직행하는 paper-with-me 고유 동선. 'DeepSeek-V3.2'처럼 리포트가 기반
    버전('DeepSeek-V3')으로만 존재하는 경우가 많아 제목 매치를 꼬리부터
    점진 완화하며, 매치 실패는 링크 없음으로 조용히 끝난다.
    """
    if model_name in _agent_paper_cache:
        return _agent_paper_cache[model_name]
    # FTS는 trigram(부분 문자열 매치) — 모델명 꼬리(버전 조각)를 점진적으로
    # 잘라가며 제목 부분 문자열로 찾는다. 'gpt' 같은 3자 일반어까지
    # 내려가면 오매칭이 늘어 후보는 4자 이상·최대 3회로 제한한다.
    base = model_name.split(" (")[0].strip().replace('"', "").lower()
    cands, s = [], base
    while s and len(cands) < 3:
        if len(s) >= (3 if not cands else 4):
            cands.append(s)
        stripped = re.sub(r"[\s\-._]+[^\s\-._]*$", "", s)
        s = stripped if stripped != s else ""
    url = None
    for cand in cands:
        row = conn.execute(
            """SELECT p.paper_url FROM papers_fts f
               JOIN papers p ON p.rowid = f.rowid
               WHERE papers_fts MATCH ? ORDER BY rank LIMIT 1""",
            (f'title:"{cand}"',)).fetchone()
        # 위치 인덱스: row_factory 유무(웹앱 Row/수집기 tuple)에 무관
        if row and row[0]:
            url = "/paper/" + paper_slug(row[0])
            break
    _agent_paper_cache[model_name] = url
    return url


def _board_rows(conn, dataset: str, metric: str) -> tuple[list[dict], str]:
    rows = conn.execute(
        "SELECT task, model_name, metrics, paper_date FROM sota_rows "
        "WHERE source = 'external' AND dataset = ?", (dataset,),
    ).fetchall()
    parsed, task = [], None
    for r in rows:
        task = task or r["task"]
        try:
            v = float(json.loads(r["metrics"]).get(metric))
        except (TypeError, ValueError):
            continue
        # 가격/비용 0은 '미책정' — 구 스냅샷에 남은 0행이 보드를
        # $0로 도배하지 않게 읽기 시점에도 거른다
        if v <= 0 and "USD" in metric:
            continue
        parsed.append({"model": r["model_name"], "value": v,
                       "date": r["paper_date"]})
    return parsed, task


def model_comparison(conn) -> list[dict]:
    """외부 미러(source='external')에서 모델 비교 보드를 뽑는다.
    보드별 order(asc/desc)로 정렬, top_by가 있으면 해당 지표 상위 25개
    모델로 대상을 좁힌다. 데이터가 없으면 빈 rows."""
    out = []
    for board in MODEL_BOARDS:
        dataset, metric = board["dataset"], board["metric"]
        parsed, task = _board_rows(conn, dataset, metric)
        if board.get("top_by"):
            ref, ref_metric = board["top_by"]
            ref_rows, _ = _board_rows(conn, ref, ref_metric)
            ref_rows.sort(key=lambda p: p["value"], reverse=True)
            allowed = {p["model"] for p in ref_rows[:25]}
            parsed = [p for p in parsed if p["model"] in allowed]
        parsed.sort(key=lambda p: p["value"],
                    reverse=board["order"] == "desc")
        top = parsed[:25]
        # 논문 링크는 화면에 나가는 상위 행에만 계산한다 — 전체 576개
        # 모델에 FTS를 돌리면 첫 로드가 수 초 늘어난다 (이후는 캐시)
        for p in top:
            p["paper"] = _agent_paper(conn, p["model"])
        # 병합 리더보드 직행 — 그 보드에선 논문 결과와 상용 모델이 값 순으로
        # 한 표에 선다 (학계 SOTA vs 상용 모델 비교, 이 서비스 고유)
        board_url = (f"/sota/{slugify(task)}/{slugify(dataset)}"
                     if task and top else None)
        out.append({**board, "rows": top, "board_url": board_url})
    return out


# 가성비 산점도의 Y축으로 전환 가능한 지표들 (사용자 요청: 지능·HLE·코딩)
SCATTER_METRICS = [
    ("Artificial Analysis Intelligence Index", "Index",
     "Intelligence Index"),
    ("Artificial Analysis Coding Index", "Index", "Coding Index"),
    ("Humanity's Last Exam", "Accuracy", "Humanity's Last Exam"),
]


def value_frontier(conn,
                   dataset: str = "Artificial Analysis Intelligence Index",
                   metric: str = "Index",
                   label: str = "Intelligence Index") -> dict | None:
    """지표 × 1M 토큰당 가격(혼합 3:1) 산점도 + 파레토 프런티어.

    좌표는 100×60 viewBox 기준으로 서버에서 계산한다(x: 로그 축).
    두 지표를 모두 가진 모델이 3개 미만이면 섹션을 숨긴다(None).
    """
    import math

    def _vals(dataset: str, metric: str) -> dict[str, float]:
        vals: dict[str, float] = {}
        # 위치 인덱스: row_factory 유무(웹앱 Row/수집기 tuple)에 무관하게 동작
        for r in conn.execute(
                "SELECT model_name, metrics FROM sota_rows "
                "WHERE source = 'external' AND dataset = ?", (dataset,)):
            try:
                vals[r[0]] = float(json.loads(r[1]).get(metric))
            except (TypeError, ValueError):
                continue
        return vals

    intel = _vals(dataset, metric)
    price = _vals("Price per 1M Tokens (Blended 3:1)", "USD per 1M Tokens")
    pts = [{"model": m, "intel": v, "price": price[m]}
           for m, v in intel.items() if price.get(m, 0) > 0]
    if len(pts) < 3:
        return None
    logs = [math.log10(p["price"]) for p in pts]
    llo, lhi = min(logs), max(logs)
    span = (lhi - llo) or 1.0
    ymax = max(p["intel"] for p in pts) or 1.0
    for p in pts:
        p["x"] = round(5 + 90 * (math.log10(p["price"]) - llo) / span, 2)
        p["y"] = round(50 - 44 * p["intel"] / ymax, 2)
        # 점 클릭 → 논문 이동 (사용자 요청). 전 모델 FTS는 첫 로드를 수 초
        # 늘리므로 보드 계산에서 이미 캐시된 모델만 링크한다
        p["paper"] = _agent_paper_cache.get(p["model"])
    pts.sort(key=lambda p: (p["price"], -p["intel"]))
    best, frontier = float("-inf"), []
    for p in pts:
        p["frontier"] = p["intel"] > best
        if p["frontier"]:
            best = p["intel"]
            frontier.append(p)
    for p in frontier:
        # 프런티어 점(라벨 노출·클릭 1순위)은 정식 조회 — 수십 개뿐이라 저렴
        p["paper"] = _agent_paper(conn, p["model"])
    path = " ".join(
        f"{'M' if i == 0 else 'L'} {p['x']},{p['y']}"
        for i, p in enumerate(frontier))
    ticks = []
    for e in range(math.ceil(llo), math.floor(lhi) + 1):
        ticks.append({"x": round(5 + 90 * (e - llo) / span, 2),
                      "label": f"${10 ** e:g}"})
    # Y축 눈금 — 축 라벨 없이는 산점도를 읽을 수 없다는 사용자 피드백.
    # ymax에 맞춰 3~5개 나오는 스텝을 고른다.
    ystep = next(s for s in (5, 10, 20, 25, 50) if ymax / s <= 5)
    yticks = [{"y": round(50 - 44 * t / ymax, 2), "label": t}
              for t in range(ystep, int(ymax) + 1, ystep)]
    return {"points": pts, "path": path, "ticks": ticks, "yticks": yticks,
            "label": label}


# 클라이언트 수치 필터("지표 X가 Y 이상/미만")에 노출할 지표들
AGENT_FILTER_METRICS = [
    ("Artificial Analysis Intelligence Index", "Index",
     "Intelligence Index"),
    ("Artificial Analysis Coding Index", "Index", "Coding Index"),
    ("Humanity's Last Exam", "Accuracy", "Humanity's Last Exam (%)"),
    ("AA-Omniscience Hallucination Rate", "Hallucination Rate",
     "Hallucination Rate (%)"),
    ("Price per 1M Tokens (Blended 3:1)", "USD per 1M Tokens",
     "Price (USD/1M)"),
]


def agent_metric_map(conn) -> dict:
    """모델별 지표 값 맵 — /agents의 수치 필터가 클라이언트에서 쓴다.

    {"labels": [지표명...], "models": {소문자 모델명: {지표명: 값}}}.
    데이터가 있는 지표만 labels에 올라 필터 셀렉트에 노출된다.
    """
    models: dict[str, dict[str, float]] = {}
    labels: list[str] = []
    for ds, metric, label in AGENT_FILTER_METRICS:
        found = False
        for r in conn.execute(
                "SELECT model_name, metrics FROM sota_rows "
                "WHERE source = 'external' AND dataset = ?", (ds,)):
            try:
                v = float(json.loads(r[1]).get(metric))
            except (TypeError, ValueError):
                continue
            if v <= 0 and "USD" in metric:
                continue
            models.setdefault(r[0].lower(), {})[label] = v
            found = True
        if found:
            labels.append(label)
    return {"labels": labels, "models": models}


def value_frontiers(conn) -> list[dict]:
    """전환 가능한 지표별 가성비 산점도 목록 (데이터 있는 지표만)."""
    out = []
    for ds, metric, label in SCATTER_METRICS:
        f = value_frontier(conn, ds, metric, label)
        if f:
            out.append(f)
    return out
