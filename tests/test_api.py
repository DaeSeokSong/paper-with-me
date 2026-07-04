"""공개 JSON API v1 테스트 — 모바일 앱 연동 계약."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("api") / "pwc.sqlite"
    conn = db.connect(db_path)
    ingest.ingest_all(conn, {
        "papers": FIXTURES / "papers.json",
        "links": FIXTURES / "links.json",
        "evaluations": FIXTURES / "evaluation-tables.json",
        "methods": FIXTURES / "methods.json",
        "datasets": FIXTURES / "datasets.json",
    })
    conn.close()
    return TestClient(create_app(db_path))


def test_stats(client):
    body = client.get("/api/v1/stats").json()
    assert body["papers"] == 2 and body["tasks"] == 2


def test_papers_list_pagination_shape(client):
    body = client.get("/api/v1/papers").json()
    assert set(body) == {"results", "page", "has_next"}
    assert body["page"] == 1 and body["has_next"] is False
    assert body["results"][0]["authors"]  # JSON 컬럼이 파싱되어 배열로


def test_paper_detail_includes_repos_and_results(client):
    body = client.get("/api/v1/papers/attention-is-all-you-need").json()
    assert body["arxiv_id"] == "1706.03762"
    assert len(body["repositories"]) == 2
    assert isinstance(body["results"], list)


def test_api_404_is_json(client):
    r = client.get("/api/v1/papers/no-such-paper")
    assert r.status_code == 404
    assert r.json() == {"detail": "paper not found"}
    assert "text/html" not in r.headers["content-type"]


def test_search(client):
    body = client.get("/api/v1/search", params={"q": "attention"}).json()
    assert any(p["arxiv_id"] == "1706.03762" for p in body["results"])


def test_tasks_and_benchmark(client):
    tasks = client.get("/api/v1/tasks").json()["results"]
    assert any(t["task"] == "Image Classification" for t in tasks)

    task = client.get("/api/v1/tasks/image-classification").json()
    assert task["benchmarks"][0]["dataset"] == "ImageNet"

    board = client.get("/api/v1/benchmarks/image-classification/imagenet").json()
    assert board["rows"][0]["model_name"] == "ResNet-152"
    assert board["metric_names"][0] == "Top 1 Accuracy"


def test_datasets_and_methods(client):
    d = client.get("/api/v1/datasets/imagenet").json()
    assert d["name"] == "ImageNet" and "benchmarks" in d
    m = client.get("/api/v1/methods/transformer").json()
    assert m["introduced_year"] == 2017


def test_trends_and_cors(client):
    r = client.get("/api/v1/trends", headers={"Origin": "https://example.app"})
    assert r.status_code == 200 and r.json()["years"]
    assert r.headers.get("access-control-allow-origin") == "*"


def test_openapi_docs_available(client):
    assert client.get("/openapi.json").status_code == 200
