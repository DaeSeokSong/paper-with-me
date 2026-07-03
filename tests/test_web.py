from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("web") / "pwc.sqlite"
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


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Attention Is All You Need" in r.text
    assert "Trending" in r.text


def test_paper_detail_shows_code_and_results(client):
    r = client.get("/paper/deep-residual-learning-for-image-recognition")
    assert r.status_code == 200
    assert "Deep Residual Learning" in r.text
    assert "Results" in r.text  # sota_rows 연결

    r = client.get("/paper/attention-is-all-you-need")
    assert "tensor2tensor" in r.text
    assert "공식 구현" in r.text


def test_paper_404(client):
    assert client.get("/paper/no-such-paper").status_code == 404


def test_search(client):
    r = client.get("/search", params={"q": "attention"})
    assert r.status_code == 200
    assert "Attention Is All You Need" in r.text


def test_sota_index_and_task_page(client):
    r = client.get("/sota")
    assert r.status_code == 200
    assert "Image Classification" in r.text

    # task 페이지는 벤치마크(dataset) 카드 목록
    r = client.get("/sota/image-classification")
    assert r.status_code == 200
    assert "ImageNet" in r.text
    assert "/sota/image-classification/imagenet" in r.text

    # 원본 URL 구조 /task/{slug} 도 동작
    assert client.get("/task/image-classification").status_code == 200


def test_dataset_leaderboard_page(client):
    r = client.get("/sota/image-classification/imagenet")
    assert r.status_code == 200
    assert "ResNet-152" in r.text
    assert "Top 1 Accuracy" in r.text

    # subtask 벤치마크도 독립 리더보드 페이지로 접근 가능
    r = client.get("/sota/few-shot-image-classification/mini-imagenet-5-way-1-shot")
    assert r.status_code == 200
    assert "ProtoNet" in r.text

    assert client.get("/sota/image-classification/no-such-dataset").status_code == 404


def test_datasets(client):
    r = client.get("/datasets")
    assert "ImageNet" in r.text
    r = client.get("/dataset/imagenet")
    assert r.status_code == 200
    assert "벤치마크" in r.text  # ImageNet 리더보드 연결


def test_methods(client):
    r = client.get("/methods")
    assert "Transformer" in r.text
    r = client.get("/method/transformer")
    assert r.status_code == 200
    assert "attention mechanism" in r.text


def test_trends(client):
    r = client.get("/trends")
    assert r.status_code == 200
    assert "tf" in r.text and "pytorch" in r.text
