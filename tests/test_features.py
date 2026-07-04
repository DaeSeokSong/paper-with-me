"""니즈 검증(리서치) 기반 신규 기능 테스트 — 태스크 태그, 유사 논문,
주간 다이제스트, 급상승 태스크, 메서드 용어집."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "features.sqlite"
    conn = pwc_db.connect(db_path)
    # 논문: 리더보드 있는 task + 없는 task를 함께 가짐
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,tasks,methods,source)"
        " VALUES (?,?,?,?,?,?,?)",
        ("https://paperswithcode.com/paper/feature-paper",
         "Contrastive Vision Transformers for Robust Recognition",
         "We study contrastive pretraining of vision transformers.",
         "2026-09-01",
         json.dumps(["Image Classification", "Ghost Task"]),
         json.dumps(["Transformer", "Unknown Method"]), "arxiv"),
    )
    # 유사 논문 후보 (제목 키워드 공유)
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date) VALUES (?,?,?,?)",
        ("https://paperswithcode.com/paper/similar-paper",
         "Vision Transformers at Scale", "Large scale vision transformers.",
         "2021-01-01"),
    )
    # 최근 신호 (다이제스트 정렬용)
    conn.execute(
        "INSERT INTO signals (paper_url,hf_upvotes,updated_at) VALUES (?,?,?)",
        ("https://paperswithcode.com/paper/feature-paper", 42,
         "2026-09-01T00:00:00"),
    )
    # 리더보드 (Image Classification만 존재) + area
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,area) VALUES (?,?,?,?,?,?,?)",
        ("Image Classification", "CIFAR-100", "M",
         json.dumps({"Acc": "96"}), "https://x/paper/p", "[]",
         "Computer Vision"),
    )
    # 메서드 카탈로그
    conn.execute(
        "INSERT INTO methods (url,name,description) VALUES (?,?,?)",
        ("https://x/method/transformer", "Transformer",
         "A Transformer is a model architecture that relies on attention "
         "mechanisms instead of recurrence."),
    )
    # 급상승 태스크용 월별 논문 (최근 8개월, 증가 추세)
    for month in range(1, 9):
        for i in range((month + 1) * 2):
            conn.execute(
                "INSERT INTO papers (paper_url,title,date,tasks) "
                "VALUES (?,?,?,?)",
                (f"https://paperswithcode.com/paper/rt-{month}-{i}",
                 f"RT {month} {i}", f"2026-{month:02d}-15",
                 json.dumps(["Image Classification"])),
            )
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path))


def test_card_task_tags_link_only_existing_boards(client):
    r = client.get("/papers")
    assert r.status_code == 200
    assert 'href="/task/image-classification"' in r.text
    assert 'href="/task/ghost-task"' not in r.text  # 보드 없는 태그는 라벨
    assert "Ghost Task" in r.text


def test_similar_papers_section(client):
    r = client.get("/paper/feature-paper")
    assert r.status_code == 200
    assert "Similar Papers" in r.text
    assert 'href="/paper/similar-paper"' in r.text


def test_methods_glossary(client):
    r = client.get("/paper/feature-paper")
    assert "이 논문이 사용한 방법론" in r.text
    assert "attention" in r.text  # Transformer 설명 요약
    assert 'href="/method/transformer"' in r.text
    assert "Unknown Method" in r.text  # 카탈로그 밖 메서드는 라벨로


def test_weekly_digest_page(client):
    r = client.get("/digest")
    assert r.status_code == 200
    assert "Weekly Digest" in r.text
    assert "Computer Vision" in r.text  # area 그룹
    # 신호가 있는 논문이 노출
    assert "Contrastive Vision Transformers" in r.text


def test_rising_tasks_on_trends(client):
    r = client.get("/trends")
    assert r.status_code == 200
    assert "Rising Tasks" in r.text
    assert 'href="/sota/image-classification"' in r.text
    assert "<polyline" in r.text  # 스파크라인


def test_digest_in_nav(client):
    assert 'href="/digest"' in client.get("/").text
