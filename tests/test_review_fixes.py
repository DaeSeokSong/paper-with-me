"""전체 코드 리뷰(7앵글)·UX 감사에서 나온 수정과 무비용 기능의 회귀 테스트."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "review.sqlite"
    conn = pwc_db.connect(db_path)
    conn.execute(
        "INSERT INTO papers (paper_url,arxiv_id,title,abstract,url_abs,date,"
        "authors,proceeding) VALUES (?,?,?,?,?,?,?,?)",
        ("https://paperswithcode.com/paper/cite-me", "1706.03762",
         "Attention Is All You Need", "The dominant sequence...",
         "https://arxiv.org/abs/1706.03762", "2017-06-12",
         json.dumps(["Ashish Vaswani", "Noam Shazeer"]), "NeurIPS 2017"),
    )
    # LIKE 폴백 페이지네이션용: FTS가 못 찾는 부분 문자열을 공유하는 25편
    for i in range(25):
        conn.execute(
            "INSERT INTO papers (paper_url,title,date) VALUES (?,?,?)",
            (f"https://paperswithcode.com/paper/xq{i}", f"zzXqToken{i} Study",
             "2020-01-01"),
        )
    # 값이 전부 0인 지표 (0을 결측 취급하던 버그)
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,metrics_order) VALUES (?,?,?,?,?,?,?)",
        ("Anomaly Detection", "ZeroBench", "M",
         json.dumps({"False Alarms": 0}), "https://x/paper/p", "[]",
         json.dumps(["False Alarms"])),
    )
    # 스텁 vN 파리티: 버전 접미가 붙은 arXiv 참조
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links) VALUES (?,?,?,?,?,?)",
        ("Image Classification", "CIFAR-100", "VN",
         json.dumps({"Acc": "95"}), "https://arxiv.org/abs/2010.01412",
         "[]"),
    )
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path))


def test_like_fallback_paginates(client):
    """FTS 0건→LIKE 폴백 검색의 2페이지가 비지 않는다 (리뷰 발견)."""
    r1 = client.get("/search", params={"q": "zzXqToken"})
    r2 = client.get("/search", params={"q": "zzXqToken", "page": 2})
    assert "zzXqToken" in r1.text
    assert "zzXqToken" in r2.text  # 과거엔 빈 결과


def test_zero_metric_column_survives(client):
    """모든 값이 0인 지표 컬럼이 결측 취급으로 사라지지 않는다."""
    r = client.get("/sota/anomaly-detection/zerobench")
    assert r.status_code == 200
    assert "False Alarms" in r.text


def test_stub_resolves_versioned_arxiv_slug(client):
    """vN 접미 slug가 스텁 경로에서도 열린다 (get_paper와 파리티)."""
    assert client.get("/paper/2010.01412v3").status_code == 200


def test_bibtex_export(client):
    r = client.get("/paper/cite-me.bib")
    assert r.status_code == 200
    assert "@inproceedings{" in r.text  # proceeding 있으므로
    assert "Attention Is All You Need" in r.text
    assert "Ashish Vaswani and Noam Shazeer" in r.text
    assert "eprint = {1706.03762}" in r.text
    # 버튼 노출
    assert '.bib"' in client.get("/paper/cite-me").text


def test_board_csv_export(client):
    r = client.get("/sota/anomaly-detection/zerobench.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "rank,model,False Alarms" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")
    # 보드 페이지에 다운로드 링크
    page = client.get("/sota/anomaly-detection/zerobench")
    assert ".csv" in page.text and "/api/v1/benchmarks/" in page.text


def test_atom_feed(client):
    r = client.get("/feed.xml")
    assert r.status_code == 200
    assert "application/atom+xml" in r.headers["content-type"]
    assert "<feed" in r.text and "<entry>" in r.text
    assert "zzXqToken" in r.text  # 최신 논문이 항목으로
    # 태그별 피드
    assert client.get("/feed.xml",
                      params={"task": "Image Classification"}).status_code == 200


def test_seo_and_discovery_endpoints(client):
    assert "<urlset" in client.get("/sitemap.xml").text
    assert "/sota/anomaly-detection" in client.get("/sitemap.xml").text
    assert "Sitemap:" in client.get("/robots.txt").text
    assert "OpenSearchDescription" in client.get("/opensearch.xml").text
    home = client.get("/").text
    assert 'property="og:title"' in home
    assert "/opensearch.xml" in home and "/feed.xml" in home
    # 논문 페이지 OG 오버라이드
    p = client.get("/paper/cite-me").text
    assert 'content="Attention Is All You Need"' in p


def test_api_benchmark_uses_shared_resolver(client):
    """HTML과 API가 같은 보드 해석 규칙 (변형 폴백 포함)."""
    r = client.get("/api/v1/benchmarks/anomaly-detection/zerobench")
    assert r.status_code == 200
    assert r.json()["task"] == "Anomaly Detection"


def test_search_strips_whitespace_and_suggests_tasks(client):
    r = client.get("/search", params={"q": "  NOHITXYZ  "})
    assert "“NOHITXYZ”" in r.text  # strip 적용
    assert "인기 벤치마크 둘러보기" in r.text  # 무결과 대안


def test_string_none_metric_values_scrubbed(tmp_path):
    """덤프에 문자열 'None'이 지표 값으로 남은 행 — 표에 >None<로 노출되던
    문제 (3,000페이지 크롤에서 발견). _loads에서 일괄 정화."""
    db_path = tmp_path / "nonestr.sqlite"
    conn = pwc_db.connect(db_path)
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,metrics_order) VALUES (?,?,?,?,?,?,?)",
        ("Visual Place Recognition", "KITTI", "M",
         json.dumps({"Recall@1": "91.2", "AUC": "None"}),
         "https://x/paper/p", "[]", json.dumps(["Recall@1", "AUC"])),
    )
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path))
    # model_name이 NULL인 행, 그리고 결측이 문자열 "None"으로 남은 행도
    # 실재한다(덤프 오염) — 후자는 NULL 가드·`or` 폴백을 모두 통과하므로
    # 읽기 시점 정화가 없으면 표에 'None'이 그대로 노출된다
    conn2 = pwc_db.connect(db_path)
    conn2.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links) VALUES (?,?,?,?,?,?)",
        ("Visual Place Recognition", "KITTI", None,
         json.dumps({"Recall@1": "88.0"}), "https://x/paper/q", "[]"),
    )
    conn2.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "paper_title,code_links) VALUES (?,?,?,?,?,?,?)",
        ("Visual Place Recognition", "KITTI", "None",
         json.dumps({"Recall@1": "85.0"}), "https://x/paper/r", "None", "[]"),
    )
    conn2.commit()
    conn2.close()
    c = TestClient(create_app(db_path))
    r = c.get("/sota/visual-place-recognition/kitti")
    assert r.status_code == 200
    assert ">None<" not in r.text
    assert "91.2" in r.text
    assert "AUC" not in r.text  # 값이 전부 정크인 컬럼은 제거
    assert "(모델명 미상)" in r.text
    # CSV도 같은 정화를 공유한다
    csv_text = c.get("/sota/visual-place-recognition/kitti.csv").text
    assert "None" not in csv_text
