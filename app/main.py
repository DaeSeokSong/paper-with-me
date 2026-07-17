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
import re
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


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_CODE = re.compile(r"`([^`]+)`")


def render_markdown(text: str | None):
    """methods/datasets 설명의 최소 마크다운 렌더링.

    아카이브 설명문은 **굵게**·[링크](url)·`코드`·$수식$을 포함한다 —
    원문 그대로 노출하면 별표·달러 기호가 깨져 보인다. HTML은 먼저
    이스케이프하고 안전한 서브셋만 변환한다. 수식($...$)은 그대로 두어
    MathJax가 클라이언트에서 조판한다."""
    import html as _html

    from markupsafe import Markup
    if not text:
        return ""
    out = _html.escape(str(text), quote=False)
    out = _MD_LINK.sub(r'<a href="\2" rel="noopener">\1</a>', out)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    out = _MD_CODE.sub(r"<code>\1</code>", out)
    return Markup(out)


def _xml_escape(text: str) -> str:
    from xml.sax.saxutils import escape
    return escape(str(text or ""))


def _bibtex(slug: str, paper: dict) -> str:
    """papers 테이블 필드만으로 BibTeX 항목 생성 (외부 의존 없음)."""
    authors = " and ".join(a for a in paper.get("authors") or []
                           if isinstance(a, str))
    year = (paper.get("date") or "")[:4]
    key = re.sub(r"[^A-Za-z0-9]+", "", slug)[:40] or "paper"
    entry = "inproceedings" if paper.get("proceeding") else "article"
    lines = [f"@{entry}{{{key},",
             f"  title = {{{paper.get('title') or slug}}},"]
    if authors:
        lines.append(f"  author = {{{authors}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if paper.get("proceeding"):
        lines.append(f"  booktitle = {{{paper['proceeding']}}},")
    if paper.get("arxiv_id"):
        lines.append(f"  eprint = {{{paper['arxiv_id']}}},")
        lines.append("  archivePrefix = {arXiv},")
    if paper.get("url_abs"):
        lines.append(f"  url = {{{paper['url_abs']}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _atom_feed(base: str, title: str, papers: list) -> str:
    e = _xml_escape
    latest = next((p.get("date") for p in papers if p.get("date")), "")
    entries = []
    for p in papers:
        link = f"{base}/paper/{queries.paper_slug(p.get('paper_url'))}"
        date = (p.get("date") or "")[:10]
        summary = (p.get("abstract") or "")[:500]
        entries.append(
            f"<entry><title>{e(p.get('title'))}</title>"
            f'<link href="{e(link)}"/>'
            f"<id>{e(link)}</id>"
            f"<updated>{e(date)}T00:00:00Z</updated>"
            f"<summary>{e(summary)}</summary></entry>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>{e(title)}</title>"
            f'<link href="{e(base)}/feed.xml" rel="self"/>'
            f"<id>{e(base)}/feed.xml</id>"
            f"<updated>{e(latest[:10] or '1970-01-01')}T00:00:00Z</updated>"
            + "".join(entries) + "</feed>")


def create_app(db_path: Path | None = None) -> FastAPI:
    db_path = db_path or Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    app = FastAPI(title="paper-with-me")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["paper_slug"] = queries.paper_slug
    templates.env.filters["slugify"] = queries.slugify
    templates.env.filters["md"] = render_markdown
    templates.env.globals["PAGE_SIZE"] = queries.PAGE_SIZE
    # 리더보드 차트 x축을 현재 월까지 연장하는 데 사용 (호출 시점 평가 —
    # 장수 프로세스에서도 날짜가 고정되지 않도록 함수로 노출)
    import datetime as _dt
    templates.env.globals["current_month"] = (
        lambda: _dt.date.today().strftime("%Y-%m"))

    def conn():
        # SQLite 연결은 요청 스레드마다 새로 연다. DDL/마이그레이션 없는
        # fast 연결 — 스키마는 아래 기동 예열의 connect()가 보장한다.
        return queries.connect_fast(db_path)

    # 비정형 논문 URL 맵을 기동 시 예열 — 첫 사용자 요청이 스냅샷 스캔
    # 비용(느린 디스크에서 수 초)을 지불하지 않도록 한다. DB가 아직 없는
    # 개발 환경 등에서는 조용히 건너뛴다.
    if db_path.exists():
        try:
            warm = queries.connect(db_path)  # 스키마/마이그레이션 보장
            # 쓰기(commit)를 동반하는 구축을 먼저 — 뒤에 두면 파일 mtime이
            # 바뀌어 방금 예열한 캐시 키가 즉시 무효화된다 (코드 리뷰 발견)
            queries.ensure_papers_tasks(warm)
            # 스냅샷 캐시 예열 — 첫 방문자가 5GB 콜드 스캔을 지불하지 않게
            queries._paper_url_map(warm)
            queries.task_slugs(warm)
            queries.task_variants(warm, "")
            queries.find_paper_task(warm, "")
            queries._slug_map(warm, "dataset", "SELECT name FROM datasets")
            queries._slug_map(warm, "method", "SELECT name FROM methods")
            queries.total_papers(warm)
            queries._area_map(warm)
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
        q = q.strip()  # 공백만 다른 검색·제목 노출 방지 (포스텔의 법칙)
        papers = queries.search_papers(c, q, page) if q else []
        return render(request, "search.html", q=q, paper_q=q, page=page,
                      papers=papers, missing=missing,
                      task_slugs=queries.task_slugs(c),
                      popular_tasks=(queries.sota_tasks(c)[:6]
                                     if q and not papers else None),
                      matches=queries.search_matches(c, q) if q else None)

    @app.get("/paper/{slug}.bib", include_in_schema=False)
    def paper_bibtex(slug: str):
        """BibTeX 인용 — 연구자의 핵심 반복 작업 (papers 필드만으로 생성)."""
        c = conn()
        p = queries.get_paper(c, slug) or queries.get_paper_stub(c, slug)
        if not p:
            raise HTTPException(404, "논문을 찾을 수 없습니다")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(_bibtex(slug, p),
                                 media_type="text/plain; charset=utf-8")

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
        results = (p.get("results") if p.get("stub")
                   else queries.paper_results(c, p["paper_url"]))
        return render(request, "paper.html", paper=p,
                      linkable_tasks=linkable,
                      similar=queries.similar_papers(c, p),
                      glossary=queries.methods_glossary(c, p["methods"]),
                      task_slugs=queries.task_slugs(c),
                      repos=(p.get("repos")
                             if p.get("stub")
                             else queries.paper_repos(c, p["paper_url"])),
                      results=results,
                      # 원본 논문 페이지의 'Ranked #N' 배지
                      result_ranks=queries.paper_result_ranks(c, results))

    @app.get("/papers", response_class=HTMLResponse)
    def papers(request: Request, page: Page = 1, task: str = ""):
        c = conn()
        if task:
            # 태그(task) 기준 논문 목록 — papers_tasks 역인덱스 사용
            items, total = queries.papers_by_task(c, task, page)
        else:
            items, total = (queries.latest_papers(c, page),
                            queries.total_papers(c))
        return render(request, "papers.html", page=page, papers=items,
                      task=task, task_slugs=queries.task_slugs(c),
                      total=total)

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
            # 리더보드는 없지만 논문 태그로는 존재하는 task → 태그 검색으로
            tagged = queries.find_paper_task(c, task_slug)
            if tagged:
                return RedirectResponse(
                    "/papers?task=" + urllib.parse.quote(tagged),
                    status_code=302)
            return _search_fallback(task_slug)
        # 같은 slug의 task 표기 변형(대소문자 등)을 병합해 벤치마크 누락 방지
        variants = queries.task_variants(c, task_slug)
        # 원본 task 페이지의 Papers 목록·Most implemented 섹션 재현
        recent, total = queries.papers_by_task(c, task, 1)
        return render(request, "task.html", task=task, task_slug=task_slug,
                      benchmarks=queries.task_benchmarks(c, task, variants),
                      recent_papers=recent[:6], total_papers=total,
                      most_implemented=queries.most_implemented(c, task),
                      task_slugs=queries.task_slugs(c))

    # 원본 사이트의 /task/{slug} URL 호환 — 중복 콘텐츠를 피하기 위해
    # 정규 URL(/sota/{slug})로 리다이렉트한다
    @app.get("/task/{task_slug}")
    def task_alias(task_slug: str):
        return RedirectResponse(f"/sota/{task_slug}", status_code=301)

    @app.get("/sota/{task_slug}/{dataset_slug}.csv", include_in_schema=False)
    def sota_board_csv(task_slug: str, dataset_slug: str):
        """리더보드 CSV — 표를 스프레드시트·논문으로 가져가는 수요."""
        import csv
        import io
        c = conn()
        task, dataset = queries.resolve_board(c, task_slug, dataset_slug)
        if not task or dataset is None:
            raise HTTPException(404, "벤치마크를 찾을 수 없습니다")
        board = queries.dataset_leaderboard(c, task, dataset)
        ranks = queries.board_ranks(
            board["rows"],
            board["metric_names"][0] if board["metric_names"] else None)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["rank", "model"] + board["metric_names"]
                   + ["extra_training_data", "paper_title", "paper_url",
                      "year", "source"])
        for rank, r in zip(ranks, board["rows"]):
            metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) \
                else {}
            w.writerow([rank, r.get("model_name") or ""]
                       + [metrics.get(m, "") for m in board["metric_names"]]
                       + [r.get("uses_additional_data") or "",
                          r.get("paper_title") or "",
                          r.get("paper_url") or "",
                          (r.get("paper_date") or "")[:4],
                          r.get("source") or "archive"])
        from fastapi.responses import Response
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{task_slug}'
                                 f'-{dataset_slug}.csv"'})

    @app.get("/sota/{task_slug}/{dataset_slug}", response_class=HTMLResponse)
    def sota_board(request: Request, task_slug: str, dataset_slug: str,
                   page: Page = 1, per: int = 20):
        # 페이지당 행 수 — 대형 벤치마크(수백~천 행)의 스크롤 부담을 줄인다
        if per not in queries.BOARD_PAGE_SIZES:
            per = queries.BOARD_PAGE_SIZE
        c = conn()
        task, dataset = queries.resolve_board(c, task_slug, dataset_slug)
        if not task:
            raise HTTPException(404, "task를 찾을 수 없습니다")
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
    def datasets(request: Request, q: str = "", page: Page = 1,
                 mod: str = "", lang: str = ""):
        # 원본 /datasets의 좌측 필터 패널 (모달리티/언어) 재현
        c = conn()
        return render(request, "datasets.html", q=q, page=page,
                      mod=mod, lang=lang,
                      facets=queries.dataset_facets(c, q),
                      datasets=queries.list_datasets(c, q, page, mod, lang))

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
    def methods(request: Request, q: str = "", page: Page = 1,
                col: str = ""):
        # 원본 Methods 인덱스의 카테고리(컬렉션) 탐색 재현
        c = conn()
        return render(request, "methods.html", q=q, page=page, col=col,
                      collections=queries.method_collections(c),
                      methods=queries.list_methods(c, q, page, col))

    @app.get("/method/{slug}", response_class=HTMLResponse)
    def method(request: Request, slug: str):
        c = conn()
        m = queries.get_method(c, slug)
        if not m:
            return _search_fallback(slug, kind="method")
        return render(request, "method.html", method=m)

    @app.get("/feed.xml", include_in_schema=False)
    def feed(request: Request, task: str = ""):
        """Atom 피드 — 최신 논문(또는 태그별). RSS 리더 구독용."""
        c = conn()
        if task:
            items, _ = queries.papers_by_task(c, task, 1)
        else:
            items = queries.latest_papers(c, 1)
        base = str(request.base_url).rstrip("/")
        title = f"paper-with-me — {task}" if task else "paper-with-me — 최신 논문"
        from fastapi.responses import Response
        return Response(_atom_feed(base, title, items),
                        media_type="application/atom+xml")

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap(request: Request):
        """카탈로그 사이트맵 — 정적 페이지 + task/데이터셋/방법론.
        (논문 57만 건은 50k URL 한도 초과라 카탈로그부터; 전량은 분할 확장)"""
        c = conn()
        base = str(request.base_url).rstrip("/")
        urls = [f"{base}{p}" for p in
                ("/", "/papers", "/sota", "/datasets", "/methods",
                 "/trends", "/digest")]
        urls += [f"{base}/sota/{s}" for s in sorted(queries.task_slugs(c))]
        urls += [f"{base}/dataset/{s}" for s in sorted(queries._slug_map(
            c, "dataset", "SELECT name FROM datasets"))]
        urls += [f"{base}/method/{s}" for s in sorted(queries._slug_map(
            c, "method", "SELECT name FROM methods"))]
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + "".join(f"<url><loc>{_xml_escape(u)}</loc></url>"
                          for u in urls[:50000])
                + "</urlset>")
        from fastapi.responses import Response
        return Response(body, media_type="application/xml")

    @app.get("/robots.txt", include_in_schema=False)
    def robots(request: Request):
        from fastapi.responses import PlainTextResponse
        base = str(request.base_url).rstrip("/")
        return PlainTextResponse(
            f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")

    @app.get("/opensearch.xml", include_in_schema=False)
    def opensearch(request: Request):
        """브라우저 주소창 검색 등록용 디스크립터."""
        base = str(request.base_url).rstrip("/")
        from fastapi.responses import Response
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<OpenSearchDescription '
            'xmlns="http://a9.com/-/spec/opensearch/1.1/">'
            "<ShortName>paper-with-me</ShortName>"
            "<Description>ML 논문·코드·SOTA 검색</Description>"
            f'<Url type="text/html" '
            f'template="{base}/search?q={{searchTerms}}"/>'
            "</OpenSearchDescription>",
            media_type="application/opensearchdescription+xml")

    @app.get("/digest", response_class=HTMLResponse)
    def digest(request: Request,
               year: Annotated[int, Query(ge=1990, le=2100)] | None = None,
               week: Annotated[int, Query(ge=1, le=53)] | None = None):
        c = conn()
        return render(request, "digest.html",
                      digest=queries.weekly_digest(c, year, week),
                      task_slugs=queries.task_slugs(c))

    @app.get("/agents", response_class=HTMLResponse)
    def agents(request: Request):
        """AI Agents 비교 — Artificial Analysis 원본 지표 미러 +
        가성비 프런티어·논문 직행(paper-with-me 고유)."""
        c = conn()
        return render(request, "models.html",
                      boards=queries.model_comparison(c),
                      frontier=queries.value_frontier(c))

    @app.get("/models", include_in_schema=False)
    def models_alias():
        return RedirectResponse("/agents", status_code=301)

    @app.get("/trends", response_class=HTMLResponse)
    def trends(request: Request):
        c = conn()
        return render(request, "trends.html",
                      trends=queries.framework_trends(c),
                      rising=queries.rising_tasks(c))

    return app


app = create_app()
