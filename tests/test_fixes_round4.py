"""사용자 보고 4건 회귀 테스트 — 미래 날짜 오염, 마크다운/수식 렌더링,
태그 기준 논문 검색, 검색 자동완성."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, render_markdown
from pwc import db as pwc_db


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "r4.sqlite"
    conn = pwc_db.connect(db_path)
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,tasks,source) "
        "VALUES (?,?,?,?,?,?)",
        ("https://paperswithcode.com/paper/good-paper", "Good Paper",
         "A normal paper.", "2026-06-30",
         json.dumps(["Image Classification"]), "arxiv"),
    )
    # 아카이브 실데이터에서 발견된 오타 미래 날짜 행 (제목 'xx', 2222년)
    conn.execute(
        "INSERT INTO papers (paper_url,title,date) VALUES (?,?,?)",
        ("https://paperswithcode.com/paper/junk-future", "xx", "2222-12-22"),
    )
    conn.execute(
        "INSERT INTO methods (url,name,description) VALUES (?,?,?)",
        ("https://x/method/linear-layer", "Linear Layer",
         "A **Linear Layer** is a projection $\\mathbf{XW + b}$. "
         "See [docs](https://example.com/docs)."),
    )
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links) VALUES (?,?,?,?,?,?)",
        ("Image Classification", "CIFAR-100", "ResNet",
         json.dumps({"Acc": "91"}), "https://x/paper/p", "[]"),
    )
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path))


def test_future_dated_junk_hidden_from_recency(client):
    """'2222-12-22' 오타 행이 최신 목록·다이제스트를 오염시키지 않는다."""
    r = client.get("/papers")
    assert "Good Paper" in r.text
    assert "2222-12-22" not in r.text  # 미래 날짜 행 제외

    r = client.get("/digest")
    assert r.status_code == 200
    assert "2222-12" not in r.text  # 앵커가 미래 행을 따라가지 않음
    assert "Good Paper" in r.text


def test_markdown_and_math_rendering():
    html = str(render_markdown(
        "A **Linear Layer** is $\\mathbf{XW + b}$ with `code` and "
        "[docs](https://example.com/d)."))
    assert "<b>Linear Layer</b>" in html
    assert "$\\mathbf{XW + b}$" in html  # 수식은 MathJax용으로 원문 보존
    assert "<code>code</code>" in html
    assert '<a href="https://example.com/d" rel="noopener">docs</a>' in html
    # XSS 방지 — 태그는 이스케이프
    assert "<script>" not in str(render_markdown("<script>alert(1)</script>"))


def test_method_page_renders_markdown_and_loads_mathjax(client):
    r = client.get("/method/linear-layer")
    assert r.status_code == 200
    assert "<b>Linear Layer</b>" in r.text  # ** 원문 노출 금지
    assert "**" not in r.text.split("<footer")[0].split("</h1>")[1]
    assert "mathjax-tex-svg.js" in r.text  # 수식 조판 스크립트


def test_tag_based_paper_search(client):
    r = client.get("/papers", params={"task": "Image Classification"})
    assert r.status_code == 200
    assert "Good Paper" in r.text
    assert "태그가 달린 논문" in r.text and "필터 해제" in r.text
    # 없는 태그는 빈 목록 (오류 없음)
    r = client.get("/papers", params={"task": "No Such Task"})
    assert r.status_code == 200


def test_suggest_api(client):
    # 'res' → ResNet 방법론/태스크류 카탈로그 + 논문 제목
    conn_r = client.get("/api/v1/suggest", params={"q": "imag"})
    assert conn_r.status_code == 200
    results = conn_r.json()["results"]
    assert any(x["label"] == "Image Classification" for x in results)
    assert all({"label", "url", "kind"} <= set(x) for x in results)

    r = client.get("/api/v1/suggest", params={"q": "good"})
    labels = [x["label"] for x in r.json()["results"]]
    assert "Good Paper" in labels  # 논문 제목 자동완성

    assert client.get("/api/v1/suggest", params={"q": ""}).json()["results"] == []


def test_autocomplete_wired_in_header(client):
    html = client.get("/").text
    assert 'id="suggest-box"' in html
    assert "/api/v1/suggest?q=" in html


def test_digest_week_navigation(client):
    """다이제스트 아카이브 — 특정 주차 조회 + 이전/다음 주 링크."""
    # 기본: 최신 논문(2026-06-30, ISO 27주차)이 속한 주
    r = client.get("/digest")
    assert "2026년 27주차" in r.text
    assert "Good Paper" in r.text
    assert "이전 주" in r.text

    # 과거 주차 직접 조회 — 빈 주는 안내와 탐색 링크
    r = client.get("/digest", params={"year": 2026, "week": 20})
    assert r.status_code == 200
    assert "2026년 20주차" in r.text
    assert "이 주에 수집된 논문이 없습니다" in r.text
    assert "다음 주 →" in r.text  # 앵커 이전 주차라 다음 주 링크 존재

    # 범위 밖 값은 앵커 주로 폴백 (500 금지)
    assert client.get("/digest", params={"year": 2026, "week": 53}).status_code == 200


def test_auto_tagging_collected_papers(tmp_path):
    """수집 논문(tasks 빈)에 본문 언급 task명을 자동 태깅 —
    태그 카테고라이징·검색이 수집 논문에도 작동하게 하는 기반."""
    from pwc.collectors import auto_tag

    conn = pwc_db.connect(tmp_path / "tag.sqlite")
    for task in ("Image Classification", "Object Detection",
                 "Few-Shot Image Classification"):
        conn.execute(
            "INSERT INTO sota_rows (task,dataset,model_name,metrics,"
            "code_links) VALUES (?,?,?,?,?)",
            (task, "DS", "M", "{}", "[]"),
        )
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,source) "
        "VALUES (?,?,?,?,?)",
        ("https://paperswithcode.com/paper/tagme",
         "Prototype Networks for Few-Shot Image Classification",
         "We study few-shot image classification and object detection.",
         "2026-06-01", "arxiv"),
    )
    # 아카이브 논문은 손대지 않는다
    conn.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,source) "
        "VALUES (?,?,?,?,'archive')",
        ("https://paperswithcode.com/paper/archived",
         "Image Classification Survey", "Survey.", "2020-01-01"),
    )
    conn.commit()
    assert auto_tag.collect(conn) == 1
    tags = json.loads(conn.execute(
        "SELECT tasks FROM papers WHERE paper_url LIKE '%tagme'"
    ).fetchone()[0])
    # 더 구체적인 이름 우선, 그 부분 문자열("Image Classification")은 제외
    assert "Few-Shot Image Classification" in tags
    assert "Object Detection" in tags
    assert "Image Classification" not in tags
    assert conn.execute(
        "SELECT tasks FROM papers WHERE paper_url LIKE '%archived'"
    ).fetchone()[0] is None
    # 재실행은 no-op (이미 태깅된 논문 유지)
    assert auto_tag.collect(conn) == 0
    conn.close()
