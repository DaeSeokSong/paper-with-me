"""원본 PWC UI 동등성 잔여 격차 구현의 회귀 테스트 —
Tags 컬럼, 데이터셋 필터 패널, Methods 컬렉션, task 페이지 섹션,
Ranked #N 배지, Lato 웹폰트."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "parity.sqlite"
    conn = pwc_db.connect(db_path)
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,tasks,authors,"
        "methods) VALUES (?,?,?,?,?,?,?)",
        ("https://x/paper/best", "Best Model Paper", "We are the best.",
         "2020-01-01", json.dumps(["Image Classification"]),
         json.dumps(["Kim"]), "[]"),
    )
    conn.execute(
        "INSERT INTO papers (paper_url,title,date,tasks,authors,methods) "
        "VALUES (?,?,?,?,?,?)",
        ("https://x/paper/second", "Second Paper", "2021-01-01",
         json.dumps(["Image Classification"]), "[]", "[]"),
    )
    # 구현 저장소 — Most implemented 정렬 확인용 (best가 2개로 우위)
    for repo in ("https://github.com/a/one", "https://github.com/a/two"):
        conn.execute(
            "INSERT INTO repos (paper_url, repo_url) VALUES (?,?)",
            ("https://x/paper/best", repo))
    conn.execute(
        "INSERT INTO repos (paper_url, repo_url) VALUES (?,?)",
        ("https://x/paper/second", "https://github.com/b/one"))
    # 리더보드 — tags 있는 행과 없는 행
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,metrics_order,tags) VALUES (?,?,?,?,?,?,?,?)",
        ("Image Classification", "TinyNet", "BestNet",
         json.dumps({"Accuracy": "99.1"}), "https://x/paper/best", "[]",
         json.dumps(["Accuracy"]), json.dumps(["Self-Supervised"])),
    )
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links) VALUES (?,?,?,?,?,?)",
        ("Image Classification", "TinyNet", "SecondNet",
         json.dumps({"Accuracy": "97.0"}), "https://x/paper/second", "[]"),
    )
    # 데이터셋 — 필터 패널 (모달리티/언어)
    conn.execute(
        "INSERT INTO datasets (url,name,modalities,languages,num_papers) "
        "VALUES (?,?,?,?,?)",
        ("https://x/dataset/imgset", "ImgSet",
         json.dumps(["Images"]), "[]", 10))
    conn.execute(
        "INSERT INTO datasets (url,name,modalities,languages,num_papers) "
        "VALUES (?,?,?,?,?)",
        ("https://x/dataset/korset", "KorSet",
         json.dumps(["Texts"]), json.dumps(["Korean"]), 5))
    # 방법론 — 컬렉션 카드
    conn.execute(
        "INSERT INTO methods (url,name,collections,num_papers) VALUES (?,?,?,?)",
        ("https://paperswithcode.com/method/resnet", "ResNet",
         json.dumps([{"collection": "Convolutional Neural Networks"}]), 100))
    conn.execute(
        "INSERT INTO methods (url,name,collections,num_papers) VALUES (?,?,?,?)",
        ("https://paperswithcode.com/method/bert", "BERT",
         json.dumps([{"collection": "Language Models"}]), 90))
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path))


def test_board_tags_column(client):
    r = client.get("/sota/image-classification/tinynet")
    assert r.status_code == 200
    assert "<th>Tags</th>" in r.text
    assert "Self-Supervised" in r.text


def test_dataset_filter_panel_and_filtering(client):
    r = client.get("/datasets")
    assert "Filter by Modality" in r.text
    assert "Images" in r.text and "Texts" in r.text
    # 모달리티 필터 적용 시 해당 데이터셋만
    r = client.get("/datasets", params={"mod": "Images"})
    assert "ImgSet" in r.text and "KorSet" not in r.text
    # 언어 필터
    r = client.get("/datasets", params={"lang": "Korean"})
    assert "KorSet" in r.text and "ImgSet" not in r.text
    # API에도 같은 필터
    api = client.get("/api/v1/datasets", params={"mod": "Texts"}).json()
    assert [d["name"] for d in api["results"]] == ["KorSet"]


def test_methods_collection_cards_and_filter(client):
    r = client.get("/methods")
    assert "Convolutional Neural Networks" in r.text
    r = client.get("/methods", params={"col": "Language Models"})
    assert "BERT" in r.text and "ResNet" not in r.text


def test_task_page_sections(client):
    r = client.get("/sota/image-classification")
    assert r.status_code == 200
    assert "Most implemented" in r.text
    assert "Best Model Paper" in r.text  # 구현 2개로 최상위
    assert "Papers" in r.text  # 최신 논문 섹션


def test_paper_ranked_badge(client):
    r = client.get("/paper/best")
    assert r.status_code == 200
    assert "#1" in r.text and "ranked-badge" in r.text
    r2 = client.get("/paper/second")
    assert "#2" in r2.text


def test_chart_axis_extends_to_current_month(tmp_path):
    """옛 결과뿐인 보드도 차트 x축이 현재 월까지 연장된다 — '살아있는
    보드'가 축에서 드러나고, 마지막 점 이후 여백이 새 SOTA 부재를 보여줌."""
    import datetime

    db2 = tmp_path / "axis.sqlite"
    c2 = pwc_db.connect(db2)
    for i, (m, v, d) in enumerate([("A", "90.0", "2019-01-01"),
                                   ("B", "92.0", "2020-01-01"),
                                   ("C", "94.0", "2021-01-01")]):
        c2.execute(
            "INSERT INTO sota_rows (task,dataset,model_name,metrics,"
            "paper_url,paper_date,code_links,metrics_order) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("Old Task", "OldSet", m, json.dumps({"Acc": v}),
             f"https://x/p{i}", d, "[]", json.dumps(["Acc"])))
    c2.commit()
    c2.close()
    r = TestClient(create_app(db2)).get("/sota/old-task/oldset")
    assert r.status_code == 200
    assert datetime.date.today().strftime("%Y-%m") in r.text  # 축 라벨
    assert "2021" in r.text  # 데이터 자체는 그대로


def test_lato_webfont_served(client):
    assert "Lato" in client.get("/").text  # @font-face 선언
    f = client.get("/static/fonts/lato-regular.woff2")
    assert f.status_code == 200
    assert f.headers["content-type"] == "font/woff2"


def test_board_without_tags_has_no_tags_column(client, tmp_path):
    """tags가 전혀 없는 보드(구 스냅샷)는 빈 Tags 컬럼을 만들지 않는다."""
    r = client.get("/sota/image-classification/tinynet")
    assert r.status_code == 200  # tags 있는 보드는 위에서 검증
    db2 = tmp_path / "notags.sqlite"
    c2 = pwc_db.connect(db2)
    c2.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links) VALUES (?,?,?,?,?,?)",
        ("T", "D", "M", json.dumps({"A": "1"}), "https://x/p", "[]"))
    c2.commit()
    c2.close()
    r2 = TestClient(create_app(db2)).get("/sota/t/d")
    assert "<th>Tags</th>" not in r2.text
