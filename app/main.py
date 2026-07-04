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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import queries

TEMPLATES_DIR = Path(__file__).parent / "templates"

# page 파라미터: 음수/0은 OFFSET 왜곡, 초대형 정수는 SQLite OverflowError(500)
Page = Annotated[int, Query(ge=1, le=100_000)]


def create_app(db_path: Path | None = None) -> FastAPI:
    db_path = db_path or Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    app = FastAPI(title="paper-with-me")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["paper_slug"] = queries.paper_slug
    templates.env.filters["slugify"] = queries.slugify

    def conn():
        # SQLite 연결은 요청 스레드마다 새로 연다 (읽기 전용이라 저렴)
        return queries.connect(db_path)

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, template, ctx)

    # 브라우저 사용자가 raw JSON 에러를 보지 않도록 HTML 에러 페이지를 렌더링
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return templates.TemplateResponse(
            request, "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return templates.TemplateResponse(
            request, "error.html",
            {"status_code": 400, "detail": "잘못된 요청 파라미터입니다"},
            status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        c = conn()
        return render(request, "index.html",
                      trending=queries.trending_papers(c),
                      latest=queries.latest_papers(c),
                      stats=queries.stats(c))

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", page: Page = 1):
        c = conn()
        papers = queries.search_papers(c, q, page) if q else []
        return render(request, "search.html", q=q, paper_q=q, page=page,
                      papers=papers)

    @app.get("/paper/{slug}", response_class=HTMLResponse)
    def paper(request: Request, slug: str):
        c = conn()
        p = queries.get_paper(c, slug)
        if not p:
            raise HTTPException(404, "논문을 찾을 수 없습니다")
        return render(request, "paper.html", paper=p,
                      repos=queries.paper_repos(c, p["paper_url"]),
                      results=queries.paper_results(c, p["paper_url"]))

    @app.get("/papers", response_class=HTMLResponse)
    def papers(request: Request, page: Page = 1):
        c = conn()
        return render(request, "papers.html", page=page,
                      papers=queries.latest_papers(c, page))

    @app.get("/sota", response_class=HTMLResponse)
    def sota(request: Request):
        c = conn()
        return render(request, "sota.html", tasks=queries.sota_tasks(c))

    def _search_fallback(slug: str) -> RedirectResponse:
        # 논문 메타데이터의 task/method가 리더보드·카탈로그에 없는 경우가
        # 많다(아카이브 특성). 죽은 404 대신 검색으로 안내한다.
        q = urllib.parse.quote(slug.replace("-", " "))
        return RedirectResponse(f"/search?q={q}", status_code=302)

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
    def sota_board(request: Request, task_slug: str, dataset_slug: str):
        c = conn()
        task = queries.find_task(c, task_slug)
        if not task:
            raise HTTPException(404, "task를 찾을 수 없습니다")
        dataset = queries.find_benchmark_dataset(c, task, dataset_slug)
        if dataset is None:
            raise HTTPException(404, "벤치마크를 찾을 수 없습니다")
        return render(request, "board.html", task=task, task_slug=task_slug,
                      board=queries.dataset_leaderboard(c, task, dataset))

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
                      boards=queries.dataset_leaderboards(c, d["name"]))

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
            return _search_fallback(slug)
        return render(request, "method.html", method=m)

    @app.get("/trends", response_class=HTMLResponse)
    def trends(request: Request):
        c = conn()
        return render(request, "trends.html", trends=queries.framework_trends(c))

    return app


app = create_app()
