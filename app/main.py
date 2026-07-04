"""FastAPI 앱 — 원본 paperswithcode.com의 URL 구조를 따른다.

/                     홈 (트렌딩/최신 논문)
/search?q=            통합 검색
/paper/{slug}         논문 상세 (초록, 코드 구현, 결과)
/sota                 Browse State-of-the-Art (task 목록)
/sota/{task}          task 리더보드 (dataset별 순위표)
/datasets, /dataset/{slug}
/methods, /method/{slug}
/trends               프레임워크 점유율 추이
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import queries
from .api import build_router

STATIC_DIR = Path(__file__).parent / "static"

TEMPLATES_DIR = Path(__file__).parent / "templates"

# page 파라미터: 음수/0은 OFFSET 왜곡, 초대형 정수는 SQLite OverflowError(500)
Page = Annotated[int, Query(ge=1, le=100_000)]


def create_app(db_path: Path | None = None) -> FastAPI:
    db_path = db_path or Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    app = FastAPI(title="paper-with-me")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["paper_slug"] = queries.paper_slug
    templates.env.filters["slugify"] = queries.slugify
    templates.env.globals["PAGE_SIZE"] = queries.PAGE_SIZE

    def conn():
        # SQLite 연결은 요청 스레드마다 새로 연다 (읽기 전용이라 저렴)
        return queries.connect(db_path)

    # 비정형 논문 URL 맵을 기동 시 예열 — 첫 사용자 요청이 스냅샷 스캔
    # 비용(느린 디스크에서 수 초)을 지불하지 않도록 한다. DB가 아직 없는
    # 개발 환경 등에서는 조용히 건너뛴다.
    if db_path.exists():
        try:
            queries._paper_url_map(queries.connect(db_path))
        except Exception:  # noqa: BLE001 - 예열 실패가 기동을 막으면 안 됨
            pass

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, template, ctx)

    # 브라우저 사용자가 raw JSON 에러를 보지 않도록 HTML 에러 페이지를
    # 렌더링한다. 단, API 경로는 클라이언트(앱)가 파싱할 JSON을 유지한다.
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail},
                                status_code=exc.status_code)
        return templates.TemplateResponse(
            request, "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return templates.TemplateResponse(
            request, "error.html",
            {"status_code": 400, "detail": "잘못된 요청 파라미터입니다"},
            status_code=400)

    # 모바일 앱·외부 연동용 공개 API (읽기 전용이라 전 오리진 허용)
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["GET"], allow_headers=["*"])
    app.include_router(build_router(conn))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 서비스워커는 파일 경로가 스코프 상한이므로 루트에서 서빙한다
    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC_DIR / "sw.js",
                            media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        c = conn()
        trending = queries.trending_papers(c)
        seen = {p["paper_url"] for p in trending}
        # 같은 논문이 Trending과 Latest에 겹쳐 보이지 않도록 한다
        latest = [p for p in queries.latest_papers(c)
                  if p["paper_url"] not in seen]
        return render(request, "index.html", trending=trending,
                      latest=latest, stats=queries.stats(c),
                      task_slugs=queries.task_slugs(c))

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", page: Page = 1,
               missing: str = ""):
        c = conn()
        papers = queries.search_papers(c, q, page) if q else []
        return render(request, "search.html", q=q, paper_q=q, page=page,
                      papers=papers, missing=missing,
                      task_slugs=queries.task_slugs(c),
                      matches=queries.search_matches(c, q) if q else None)

    @app.get("/paper/{slug}", response_class=HTMLResponse)
    def paper(request: Request, slug: str):
        c = conn()
        p = queries.get_paper(c, slug)
        if not p:
            # papers 덤프에 없어도 리더보드가 참조하는 논문이면 스텁 제공
            p = queries.get_paper_stub(c, slug)
            if not p:
                raise HTTPException(404, "논문을 찾을 수 없습니다")
        # 리더보드가 있는 task만 배지를 링크로 — 없는 task 배지가 빈
        # 검색으로 떨어지는 죽은 길을 만들지 않는다
        linkable = {t for t in p["tasks"]
                    if queries.find_task(c, queries.slugify(t))}
        return render(request, "paper.html", paper=p,
                      linkable_tasks=linkable,
                      similar=queries.similar_papers(c, p),
                      glossary=queries.methods_glossary(c, p["methods"]),
                      task_slugs=queries.task_slugs(c),
                      repos=(p.get("repos")
                             if p.get("stub")
                             else queries.paper_repos(c, p["paper_url"])),
                      results=(p.get("results")
                               if p.get("stub")
                               else queries.paper_results(c, p["paper_url"])))

    @app.get("/papers", response_class=HTMLResponse)
    def papers(request: Request, page: Page = 1):
        c = conn()
        return render(request, "papers.html", page=page,
                      papers=queries.latest_papers(c, page),
                      task_slugs=queries.task_slugs(c),
                      total=queries.total_papers(c))

    @app.get("/sota", response_class=HTMLResponse)
    def sota(request: Request, area: str = ""):
        c = conn()
        areas = queries.sota_areas(c)
        if area:
            areas = [g for g in areas if g["area"] == area] or areas
        return render(request, "sota.html", areas=areas, expanded=bool(area))

    def _search_fallback(slug: str, kind: str = "task") -> RedirectResponse:
        # 논문 메타데이터의 task/method가 리더보드·카탈로그에 없는 경우가
        # 많다(아카이브 특성). 죽은 404 대신 검색으로 안내한다.
        q = urllib.parse.quote(slug.replace("-", " "))
        return RedirectResponse(f"/search?q={q}&missing={kind}",
                                status_code=302)

    @app.get("/sota/{task_slug}", response_class=HTMLResponse)
    def sota_task(request: Request, task_slug: str):
        c = conn()
        task = queries.find_task(c, task_slug)
        if not task:
            return _search_fallback(task_slug)
        return render(request, "task.html", task=task, task_slug=task_slug,
                      benchmarks=queries.task_benchmarks(c, task))

    # 원본 사이트의 /task/{slug} URL 호환 — 중복 콘텐츠를 피하기 위해
    # 정규 URL(/sota/{slug})로 리다이렉트한다
    @app.get("/task/{task_slug}")
    def task_alias(task_slug: str):
        return RedirectResponse(f"/sota/{task_slug}", status_code=301)

    @app.get("/sota/{task_slug}/{dataset_slug}", response_class=HTMLResponse)
    def sota_board(request: Request, task_slug: str, dataset_slug: str,
                   page: Page = 1, per: int = 20):
        # 페이지당 행 수 — 대형 벤치마크(수백~천 행)의 스크롤 부담을 줄인다
        if per not in queries.BOARD_PAGE_SIZES:
            per = queries.BOARD_PAGE_SIZE
        c = conn()
        task = queries.find_task(c, task_slug)
        if not task:
            raise HTTPException(404, "task를 찾을 수 없습니다")
        dataset = queries.find_benchmark_dataset(c, task, dataset_slug)
        if dataset is None:
            raise HTTPException(404, "벤치마크를 찾을 수 없습니다")
        board = queries.dataset_leaderboard(c, task, dataset)
        # 차트·지표 컬럼 선정은 전체 행 기준, 표만 페이지 단위로 자른다
        chart = queries.board_chart(
            board["rows"],
            board["metric_names"][0] if board["metric_names"] else None)
        total = len(board["rows"])
        offset = (page - 1) * per
        ranks = queries.board_ranks(
            board["rows"],
            board["metric_names"][0] if board["metric_names"] else None)
        return render(request, "board.html", task=task, task_slug=task_slug,
                      board=board, chart=chart, total=total, offset=offset,
                      rows=board["rows"][offset:offset + per],
                      ranks=ranks[offset:offset + per],
                      page=page, per=per,
                      dataset_slug=dataset_slug,
                      dataset_in_catalog=bool(
                          queries.get_dataset(c, dataset_slug)))

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets(request: Request, q: str = "", page: Page = 1):
        c = conn()
        return render(request, "datasets.html", q=q, page=page,
                      datasets=queries.list_datasets(c, q, page))

    @app.get("/dataset/{slug}", response_class=HTMLResponse)
    def dataset(request: Request, slug: str):
        c = conn()
        d = queries.get_dataset(c, slug)
        if not d:
            raise HTTPException(404, "데이터셋을 찾을 수 없습니다")
        return render(request, "dataset.html", dataset=d,
                      boards=queries.dataset_leaderboards(
                          c, d["name"], d.get("variants")))

    @app.get("/methods", response_class=HTMLResponse)
    def methods(request: Request, q: str = "", page: Page = 1):
        c = conn()
        return render(request, "methods.html", q=q, page=page,
                      methods=queries.list_methods(c, q, page))

    @app.get("/method/{slug}", response_class=HTMLResponse)
    def method(request: Request, slug: str):
        c = conn()
        m = queries.get_method(c, slug)
        if not m:
            return _search_fallback(slug, kind="method")
        return render(request, "method.html", method=m)

    @app.get("/digest", response_class=HTMLResponse)
    def digest(request: Request):
        c = conn()
        return render(request, "digest.html",
                      digest=queries.weekly_digest(c),
                      task_slugs=queries.task_slugs(c))

    @app.get("/trends", response_class=HTMLResponse)
    def trends(request: Request):
        c = conn()
        return render(request, "trends.html",
                      trends=queries.framework_trends(c),
                      rising=queries.rising_tasks(c))

    return app


app = create_app()
