"""QA 베타 테스트(3개 관점: 신규 사용자·모바일/접근성·파워유저)에서
발견된 사용성 문제의 회귀 테스트."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db

FIXTURES = Path(__file__).parent / "fixtures"


def _sota_row(conn, task="Image Classification", dataset="CIFAR-100",
              model="M", metrics=None, paper_url="https://x/paper/p",
              title="P", date="2020-01-01", extra=None):
    conn.execute(
        "INSERT INTO sota_rows (task,parent_task,dataset,model_name,metrics,"
        "paper_url,paper_title,paper_date,code_links,uses_additional_data) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task, None, dataset, model,
         json.dumps(metrics or {"Acc": "90"}), paper_url, title, date, "[]",
         extra),
    )


@pytest.fixture()
def make_client(tmp_path):
    def build(fill):
        db_path = tmp_path / "usability.sqlite"
        conn = pwc_db.connect(db_path)
        fill(conn)
        conn.commit()
        conn.close()
        return TestClient(create_app(db_path))
    return build


def test_api_serves_stub_papers_like_html(make_client):
    """HTML은 스텁으로 열리는 논문이 API에서만 404 나던 불일치 (파워유저 QA)."""
    c = make_client(lambda conn: _sota_row(
        conn, paper_url="https://openreview.net/forum?id=QaStub1",
        title="Stub Paper"))
    assert c.get("/paper/QaStub1").status_code == 200
    r = c.get("/api/v1/papers/QaStub1")
    assert r.status_code == 200
    body = r.json()
    assert body["stub"] is True and body["title"] == "Stub Paper"
    assert "repositories" in body and "results" in body


def test_dataset_variants_connect_benchmarks(make_client):
    """카탈로그명과 리더보드 dataset 문자열이 다르면 데이터셋 페이지가
    빈 껍데기가 되던 문제 — variants 매칭 (파워유저 QA)."""
    def fill(conn):
        conn.execute(
            "INSERT INTO datasets (url,name,variants) VALUES (?,?,?)",
            ("https://x/d/ms-coco", "MS COCO",
             json.dumps(["COCO test-dev"])),
        )
        _sota_row(conn, task="Object Detection", dataset="COCO test-dev")
    c = make_client(fill)
    r = c.get("/dataset/ms-coco")
    assert r.status_code == 200
    assert 'href="/sota/object-detection/coco-test-dev"' in r.text


def test_board_links_back_to_dataset_page(make_client):
    """리더보드 → 데이터셋 카탈로그 복귀 링크 (왕복 단절, 파워유저 QA)."""
    def fill(conn):
        conn.execute("INSERT INTO datasets (url,name) VALUES (?,?)",
                     ("https://x/d/cifar-100", "CIFAR-100"))
        _sota_row(conn)
    c = make_client(fill)
    r = c.get("/sota/image-classification/cifar-100")
    assert 'href="/dataset/cifar-100"' in r.text


def test_board_tie_ranks_share_medals(make_client):
    """주 지표 동점이 1·2위로 갈려 금·은이 나뉘던 문제 — 공동 순위."""
    def fill(conn):
        _sota_row(conn, model="A", metrics={"Acc": "96.08"})
        _sota_row(conn, model="B", metrics={"Acc": "96.08"})
        _sota_row(conn, model="C", metrics={"Acc": "91.00"})
    c = make_client(fill)
    r = c.get("/sota/image-classification/cifar-100")
    assert r.text.count('class="rank-gold"') == 2  # 공동 1위 둘 다 금
    assert 'class="rank-silver"' not in r.text
    assert r.text.count('>3</span>') == 1  # 다음 순위는 3위


def test_board_extra_training_data_column(make_client):
    """원본의 Extra Training Data 컬럼 — 데이터가 있을 때만 표시."""
    def fill(conn):
        _sota_row(conn, model="Pretrained", extra=1)
        _sota_row(conn, model="Scratch", extra=0)
    c = make_client(fill)
    r = c.get("/sota/image-classification/cifar-100")
    assert "Extra Training Data" in r.text and "✓" in r.text

    c2 = make_client(lambda conn: _sota_row(conn, dataset="DS2"))
    r2 = c2.get("/sota/image-classification/ds2")
    assert "Extra Training Data" not in r2.text  # 데이터 없으면 컬럼 생략


def test_search_suggests_tasks_datasets_methods(make_client):
    """'imagenet' 검색이 리더보드·데이터셋이 있는데도 0건으로 끝나던 문제
    (신규 사용자 QA) — 통합 매치 섹션."""
    def fill(conn):
        conn.execute("INSERT INTO datasets (url,name) VALUES (?,?)",
                     ("https://x/d/imagenet", "ImageNet"))
        _sota_row(conn, dataset="ImageNet")
    c = make_client(fill)
    r = c.get("/search", params={"q": "imagenet"})
    assert r.status_code == 200
    assert 'href="/dataset/imagenet"' in r.text  # 데이터셋 안내
    assert "데이터셋에서 찾기" in r.text  # 0건 빈 상태에도 다음 행동 제공


def test_search_fallback_banner_explains_redirect(make_client):
    """task 배지 → 빈 검색으로 조용히 떨어지던 죽은 길에 안내 배너."""
    c = make_client(lambda conn: None)
    r = c.get("/sota/machine-translation", follow_redirects=True)
    assert r.status_code == 200
    assert "논문 검색으로 안내했습니다" in r.text


def test_paper_task_badges_only_link_existing_boards(make_client):
    """리더보드가 없는 task 배지는 링크 대신 라벨 (신규 사용자 QA)."""
    def fill(conn):
        conn.execute(
            "INSERT INTO papers (paper_url,title,tasks,date) VALUES (?,?,?,?)",
            ("https://paperswithcode.com/paper/qa-paper", "QA Paper",
             json.dumps(["Image Classification", "Ghost Task"]),
             "2020-01-01"),
        )
        _sota_row(conn)
    c = make_client(fill)
    r = c.get("/paper/qa-paper")
    assert 'href="/task/image-classification"' in r.text
    assert 'href="/task/ghost-task"' not in r.text
    assert "Ghost Task" in r.text  # 라벨로는 표시


def test_stub_paper_offers_external_search(make_client):
    """원문·코드 링크가 전혀 없는 스텁이 막다른 길이 되지 않도록
    외부 검색 버튼 제공 (신규 사용자 QA)."""
    c = make_client(lambda conn: _sota_row(
        conn, paper_url="https://paperswithcode.com/paper/orphan-stub",
        title="Orphan Stub"))
    r = c.get("/paper/orphan-stub")
    assert r.status_code == 200
    assert "scholar.google.com" in r.text
    assert "원문·코드 링크를 이용하세요" not in r.text  # 없는 링크 약속 금지
    # 스텁도 task 배지 허브 제공 (파워유저 QA 부수 관찰)
    assert 'href="/task/image-classification"' in r.text


def test_sota_hides_zero_benchmark_tasks(make_client):
    """'0 benchmarks' 카드가 클릭되는 함정 제거 (신규 사용자 QA)."""
    def fill(conn):
        _sota_row(conn)
        conn.execute(
            "INSERT INTO sota_rows (task,dataset,model_name,metrics,"
            "code_links) VALUES (?,?,?,?,?)",
            ("Speech Denoising", None, "M", "{}", "[]"),
        )
    c = make_client(fill)
    r = c.get("/sota")
    assert "Image Classification" in r.text
    assert "Speech Denoising" not in r.text


def test_task_case_variants_are_merged(make_client):
    """같은 slug의 task 표기 변형('Class Incremental Learning' vs
    'class-incremental learning')이 공존해도 벤치마크가 404 나지 않는다
    (3,000페이지 전수 크롤에서 발견된 실데이터 회귀)."""
    def fill(conn):
        _sota_row(conn, task="Class Incremental Learning", dataset="DS-A",
                  model="A")
        _sota_row(conn, task="class-incremental learning", dataset="DS-B",
                  model="B")
    c = make_client(fill)
    # task 페이지는 두 변형의 벤치마크를 모두 보여준다
    r = c.get("/sota/class-incremental-learning")
    assert r.status_code == 200
    assert "DS-A" in r.text and "DS-B" in r.text
    # 변형 쪽에만 있는 벤치마크 리더보드도 열린다
    assert c.get("/sota/class-incremental-learning/ds-a").status_code == 200
    assert c.get("/sota/class-incremental-learning/ds-b").status_code == 200


def test_papers_pager_shows_total_and_empty_state(make_client):
    def fill(conn):
        for i in range(3):
            conn.execute(
                "INSERT INTO papers (paper_url,title,date) VALUES (?,?,?)",
                (f"https://paperswithcode.com/paper/p{i}", f"P{i}",
                 "2020-01-01"),
            )
    c = make_client(fill)
    r = c.get("/papers")
    assert "1–3 / 3" in r.text
    r = c.get("/papers", params={"page": 999})
    assert "1페이지로" in r.text  # 빈 페이지에서 복귀 링크


def test_home_deduplicates_trending_and_latest(make_client):
    def fill(conn):
        conn.execute(
            "INSERT INTO papers (paper_url,title,date) VALUES (?,?,?)",
            ("https://paperswithcode.com/paper/dup", "Dup Paper",
             "2025-01-01"),
        )
        conn.execute(
            "INSERT INTO repos (paper_url,repo_url) VALUES (?,?)",
            ("https://paperswithcode.com/paper/dup", "https://github.com/x/r"),
        )
    c = make_client(fill)
    r = c.get("/")
    assert r.text.count(">Dup Paper</a>") == 1
