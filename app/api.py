"""공개 JSON API v1 — 모바일 앱·외부 연동용 백엔드.

원본 paperswithcode.com의 api/v1 스타일을 따르되, 페이지네이션은
{results, page, has_next} 형태로 단순화했다. 문서는 /docs (OpenAPI 자동).
읽기 전용이며 CORS가 전 오리진에 열려 있다.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from . import queries

Page = Annotated[int, Query(ge=1, le=100_000)]


def build_router(get_conn) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api-v1"])

    def paginated(results: list, page: int) -> dict:
        return {
            "results": results,
            "page": page,
            "has_next": len(results) == queries.PAGE_SIZE,
        }

    @router.get("/stats", summary="적재 통계")
    def stats():
        return queries.stats(get_conn())

    @router.get("/papers", summary="최신 논문 목록")
    def papers(page: Page = 1):
        return paginated(queries.latest_papers(get_conn(), page), page)

    @router.get("/papers/trending", summary="트렌딩 논문")
    def trending():
        return {"results": queries.trending_papers(get_conn())}

    @router.get("/papers/{slug}", summary="논문 상세 (코드·벤치마크 결과 포함)")
    def paper(slug: str):
        c = get_conn()
        p = queries.get_paper(c, slug)
        if not p:
            raise HTTPException(404, "paper not found")
        p["repositories"] = queries.paper_repos(c, p["paper_url"])
        p["results"] = queries.paper_results(c, p["paper_url"])
        return p

    @router.get("/search", summary="논문 검색 (제목·초록)")
    def search(q: str, page: Page = 1):
        return paginated(queries.search_papers(get_conn(), q, page), page)

    @router.get("/tasks", summary="벤치마크 task 목록")
    def tasks():
        return {"results": queries.sota_tasks(get_conn())}

    @router.get("/tasks/{task_slug}", summary="task의 벤치마크 목록")
    def task(task_slug: str):
        c = get_conn()
        name = queries.find_task(c, task_slug)
        if not name:
            raise HTTPException(404, "task not found")
        return {"task": name, "slug": task_slug,
                "benchmarks": queries.task_benchmarks(c, name)}

    @router.get("/benchmarks/{task_slug}/{dataset_slug}",
                summary="단일 리더보드 (원본 순서)")
    def benchmark(task_slug: str, dataset_slug: str):
        c = get_conn()
        name = queries.find_task(c, task_slug)
        if not name:
            raise HTTPException(404, "task not found")
        dataset = queries.find_benchmark_dataset(c, name, dataset_slug)
        if dataset is None:
            raise HTTPException(404, "benchmark not found")
        board = queries.dataset_leaderboard(c, name, dataset)
        return {"task": name, **board}

    @router.get("/datasets", summary="데이터셋 카탈로그")
    def datasets(q: str = "", page: Page = 1):
        return paginated(queries.list_datasets(get_conn(), q, page), page)

    @router.get("/datasets/{slug}", summary="데이터셋 상세")
    def dataset(slug: str):
        c = get_conn()
        d = queries.get_dataset(c, slug)
        if not d:
            raise HTTPException(404, "dataset not found")
        d["benchmarks"] = queries.dataset_leaderboards(c, d["name"])
        return d

    @router.get("/methods", summary="방법론 카탈로그")
    def methods(q: str = "", page: Page = 1):
        return paginated(queries.list_methods(get_conn(), q, page), page)

    @router.get("/methods/{slug}", summary="방법론 상세")
    def method(slug: str):
        m = queries.get_method(get_conn(), slug)
        if not m:
            raise HTTPException(404, "method not found")
        return m

    @router.get("/trends", summary="프레임워크 점유율 추이")
    def trends():
        return queries.framework_trends(get_conn())

    return router
