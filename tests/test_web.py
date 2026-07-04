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


def test_paper_stub_for_leaderboard_only_papers(tmp_path):
    """papers 덤프에 없지만 리더보드가 참조하는 논문은 404 대신 스텁
    전용 페이지(제목·결과·코드 링크)를 제공한다 — 페이퍼 스터디 동선 유지."""
    import json as _json

    from pwc import db as pwc_db
    db_path = tmp_path / "stub.sqlite"
    conn = pwc_db.connect(db_path)
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links) VALUES (?,?,?,?,?,?,?,?,?)",
        ("Image Classification", None, "CIFAR-100", "EffNet-L2 (SAM)",
         _json.dumps({"Percentage correct": "96.08"}),
         "https://paperswithcode.com/paper/sharpness-aware-minimization",
         "Sharpness-Aware Minimization", "2020-10-03",
         _json.dumps([{"title": "davda54/sam", "url": "https://github.com/davda54/sam"}])),
    )
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path))
    r = c.get("/paper/sharpness-aware-minimization")
    assert r.status_code == 200
    assert "Sharpness-Aware Minimization" in r.text
    assert "davda54/sam" in r.text
    assert "초록" in r.text  # 초록 부재 안내


def test_board_caps_code_links(tmp_path):
    import json as _json

    from pwc import db as pwc_db
    db_path = tmp_path / "cap.sqlite"
    conn = pwc_db.connect(db_path)
    links = [{"title": f"repo{i}", "url": f"https://github.com/x/r{i}"}
             for i in range(6)]
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links) VALUES (?,?,?,?,?,?,?,?,?)",
        ("T", None, "DS", "M", _json.dumps({"Acc": "1"}),
         "https://paperswithcode.com/paper/p", "P", "2020-01-01",
         _json.dumps(links)),
    )
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path))
    r = c.get("/sota/t/ds")
    assert "repo2" in r.text and "repo3" not in r.text
    assert "+3" in r.text  # 나머지는 논문 페이지로


def test_board_sota_chart_renders(tmp_path):
    """3개 이상 결과가 있는 벤치마크는 SOTA 추이 SVG 차트를 렌더링한다."""
    import json as _json

    from pwc import db as pwc_db
    db_path = tmp_path / "chart.sqlite"
    conn = pwc_db.connect(db_path)
    for i, (d, v) in enumerate([("2019-03-01", "88.5"), ("2020-06-01", "91.2"),
                                ("2021-09-01", "93.7"), ("2022-12-01", "95.1")]):
        conn.execute(
            "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
            "paper_url,paper_title,paper_date,code_links) VALUES (?,?,?,?,?,?,?,?,?)",
            ("T", None, "DS", f"M{i}", _json.dumps({"Accuracy": v}),
             f"https://x/paper/p{i}", f"P{i}", d, "[]"),
        )
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path))
    r = c.get("/sota/t/ds")
    assert r.status_code == 200
    assert "<svg" in r.text and "<polyline" in r.text
    # 전체 점 4개(회색) + 단조 증가라 전부 프런티어 강조점(청록) 4개 = 8
    assert r.text.count("<circle") == 8


def test_board_chart_helper():
    from app import queries

    rows = [
        {"metrics": {"Error rate": "5.2"}, "paper_date": "2020-01-01", "model_name": "A"},
        {"metrics": {"Error rate": "3.1"}, "paper_date": "2021-01-01", "model_name": "B"},
        {"metrics": {"Error rate": "4.0"}, "paper_date": "2022-01-01", "model_name": "C"},
    ]
    chart = queries.board_chart(rows, "Error rate")
    assert chart["lower_better"] is True
    # 낮을수록 좋은 지표의 frontier: 5.2 → 3.1 (4.0은 기록 갱신 아님)
    assert [p["value"] for p in chart["frontier"]] == [5.2, 3.1]
    assert queries.board_chart(rows[:2], "Error rate") is None  # 점 부족


def test_empty_metric_columns_pruned(tmp_path):
    import json as _json

    from pwc import db as pwc_db
    db_path = tmp_path / "prune.sqlite"
    conn = pwc_db.connect(db_path)
    conn.row_factory = __import__("sqlite3").Row
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links,metrics_order) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("T", None, "DS", "M", _json.dumps({"Accuracy": "96"}),
         "https://x/paper/p", "P", "2020-01-01", "[]",
         _json.dumps(["Accuracy", "PARAMS", "Top 1 Accuracy"])),
    )
    conn.commit()
    from app import queries
    board = queries.dataset_leaderboard(conn, "T", "DS")
    assert board["metric_names"] == ["Accuracy"]  # 빈 컬럼 제거
    conn.close()


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


def test_pwa_assets(client):
    assert client.get("/sw.js").status_code == 200
    assert "serviceWorker" in client.get("/").text
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/offline.html").status_code == 200


def test_trends(client):
    r = client.get("/trends")
    assert r.status_code == 200
    # 원시 코드값(tf/pytorch)이 아닌 표시명으로 렌더링
    assert "TensorFlow" in r.text and "PyTorch" in r.text
