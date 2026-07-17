"""외부 평가기관 리더보드 미러 (Artificial Analysis / Scale SEAL)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db
from pwc.collectors import external_boards

AA_PAYLOAD = {"data": [
    {"name": "FrontierModel-1", "model_creator": {"name": "LabX"},
     "release_date": "2026-03-01",
     "evaluations": {"mmlu_pro": 0.87, "gpqa": {"value": 0.71},
                     "aime": 0.92, "unknown_bench": 0.5,
                     "artificial_analysis_coding_index": 55.8,
                     "artificial_analysis_math_index": 87.2,
                     # 실응답 확정 키(2026-07-17 진단): 언더스코어 없는 표기
                     "terminalbench_hard": 0.053,
                     "lcr": 0.4367},
     "pricing": {"price_1m_blended_3_to_1": 1.925,
                 "price_1m_input_tokens": 1.1}},
    {"name": "SmallModel", "release_date": "2025-11-15",
     "evaluations": {"mmlu_pro": 55.2}},  # 이미 백분율인 값
    {"name": None, "evaluations": {"mmlu_pro": 0.5}},  # 이름 없음 → 제외
]}


@pytest.fixture()
def conn(tmp_path):
    c = pwc_db.connect(tmp_path / "ext.sqlite")
    yield c
    c.close()


def test_aa_models_mirrored_with_normalized_values(conn, monkeypatch):
    monkeypatch.setenv("AA_API_KEY", "test-key")

    def fake_urlopen(req, timeout=0):
        import io
        assert req.headers.get("X-api-key") == "test-key"
        return io.BytesIO(json.dumps(AA_PAYLOAD).encode())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    added = external_boards.collect_artificial_analysis(conn)
    conn.commit()
    # 모델1: mmlu_pro/gpqa/aime/코딩·수학 인덱스/터미널벤치/LCR + 혼합 가격,
    # 모델2: mmlu_pro
    assert added == 9
    rows = conn.execute(
        "SELECT task, dataset, model_name, metrics, paper_date FROM sota_rows "
        "WHERE source='external' ORDER BY id").fetchall()
    assert rows[0][0] == "Multi-task Language Understanding"
    assert rows[0][1] == "MMLU-Pro"
    assert rows[0][2] == "FrontierModel-1 (LabX)"
    assert json.loads(rows[0][3]) == {"Accuracy": "87"}   # 0.87 → 87
    assert rows[0][4] == "2026-03-01"                     # 차트용 날짜
    # 중첩 value, 이미-백분율 값 처리
    assert json.loads(rows[1][3]) == {"Accuracy": "71"}
    by_ds = {r[1]: json.loads(r[3]) for r in rows}
    # 인덱스류는 0~100 스케일 그대로, 가격은 pricing에서 raw USD
    assert by_ds["Artificial Analysis Coding Index"] == {"Index": "55.8"}
    assert by_ds["Artificial Analysis Math Index"] == {"Index": "87.2"}
    assert by_ds["Price per 1M Tokens (Blended 3:1)"] == {
        "USD per 1M Tokens": "1.925"}
    # 실응답 확정 키 매핑: 분수 → 백분율
    assert by_ds["Terminal-Bench Hard"] == {"Accuracy": "5.3"}
    assert by_ds["AA-LCR"] == {"Accuracy": "43.7"}  # 소수 1자리 반올림
    assert json.loads(rows[-1][3]) == {"Accuracy": "55.2"}  # SmallModel


def test_aa_skipped_without_key(conn, monkeypatch):
    monkeypatch.delenv("AA_API_KEY", raising=False)
    assert external_boards.collect_artificial_analysis(conn) == 0


def test_collect_is_stateless_mirror(conn, monkeypatch):
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,code_links,"
        "source) VALUES (?,?,?,?,?, 'external')",
        ("Old", "OldBoard", "Stale", "{}", "[]"))
    conn.commit()
    monkeypatch.delenv("AA_API_KEY", raising=False)
    monkeypatch.setattr(external_boards, "collect_scale_seal", lambda c: 0)
    external_boards.collect(conn)
    # 이전 external 행은 제거되고 (이번 소스 결과 0) 재적재 없음
    assert conn.execute(
        "SELECT COUNT(*) FROM sota_rows WHERE source='external'"
    ).fetchone()[0] == 0


def test_seal_walker_finds_leaderboards():
    tree = {"props": {"boards": [
        {"title": "Humanity's Last Exam", "results": [
            {"model": {"name": "M1"}, "score": 0.31},
            {"model": "M2", "score": 0.28},
            {"model": "M3", "rating": 0.2}]},
        {"title": "짧은목록", "results": [{"model": "X", "score": 1}]},
    ]}}
    out = []
    external_boards._walk_seal(tree, out)
    assert len(out) == 1
    name, rows = out[0]
    assert name == "Humanity's Last Exam"
    assert ("M1", 0.31) in rows and ("M2", 0.28) in rows


def test_external_rows_render_with_badge_and_value_merge(conn, tmp_path):
    """external 행이 값 순으로 병합되고 '외부' 배지가 붙는다."""
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,metrics_order) VALUES (?,?,?,?,?,?,?)",
        ("Question Answering", "GPQA Diamond", "OldPaperModel",
         json.dumps({"Accuracy": "60.0"}), "https://x/p", "[]",
         json.dumps(["Accuracy"])))
    external_boards._insert(
        conn, "Question Answering", "GPQA Diamond", "Accuracy",
        "FrontierModel-1", "71", "2026-03-01", "NLP",
        external_boards.AA_LINK)
    conn.commit()
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    r = TestClient(create_app(Path(db_file))).get(
        "/sota/question-answering/gpqa-diamond")
    assert r.status_code == 200
    assert "외부" in r.text
    assert "artificialanalysis.ai" in r.text  # 출처 링크
    # 값 병합: 71 > 60 → external이 1위
    import re
    first = re.search(r"<b>([^<]+)</b>", r.text).group(1)
    assert first == "FrontierModel-1"


def test_models_page_renders_four_charts_with_attribution(conn, tmp_path):
    """/models — AA 원본 4개 지표만 미러, 출처 고지 필수 (사용자 요청)."""
    data = [
        ("Artificial Analysis Intelligence Index", "Index", "M1", "60"),
        ("Artificial Analysis Intelligence Index", "Index", "M2", "59"),
        ("Humanity's Last Exam", "Accuracy", "M1", "53.3"),
        ("AA-Omniscience Hallucination Rate", "Hallucination Rate",
         "M2", "14"),
        ("Cost per Intelligence Index Task", "Cost per task (USD)",
         "M1", "2.75"),
    ]
    for ds, metric, model, v in data:
        external_boards._insert(
            conn, "Language Modelling", ds, metric, model, v,
            "2026-03-01", "NLP", external_boards.AA_LINK)
    conn.commit()
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    c = TestClient(create_app(Path(db_file)))
    r = c.get("/agents")
    assert r.status_code == 200
    assert "Artificial Analysis" in r.text       # 원본 고지
    assert "원본 데이터의 미러" in r.text
    assert "Last Exam" in r.text  # (아포스트로피는 HTML 이스케이프됨)
    assert "낮을수록 좋음" in r.text              # 환각률/비용 배지
    assert "$2.75" in r.text                     # 비용은 달러 표기
    assert "AI Agents" in c.get("/").text        # 네비게이션 노출
    assert c.get("/models", follow_redirects=False).status_code == 301


def _seed_value_frontier(conn):
    """지능 지수 + 가격 두 지표를 가진 모델 3개 (파레토 판정 검증용)."""
    data = [
        ("Artificial Analysis Intelligence Index", "Index",
         "DeepSeek-V3.2 (DeepSeek)", "58"),
        ("Artificial Analysis Intelligence Index", "Index", "Closed-1", "70"),
        ("Artificial Analysis Intelligence Index", "Index", "Closed-2", "40"),
        ("Price per 1M Tokens (Blended 3:1)", "USD per 1M Tokens",
         "DeepSeek-V3.2 (DeepSeek)", "0.48"),
        ("Price per 1M Tokens (Blended 3:1)", "USD per 1M Tokens",
         "Closed-1", "9"),
        ("Price per 1M Tokens (Blended 3:1)", "USD per 1M Tokens",
         "Closed-2", "3"),
    ]
    for ds, metric, model, v in data:
        external_boards._insert(
            conn, "Language Modelling", ds, metric, model, v,
            "2026-03-01", "NLP", external_boards.AA_LINK)
    conn.commit()


def test_value_frontier_pareto(conn):
    from app import queries

    _seed_value_frontier(conn)
    f = queries.value_frontier(conn)
    flags = {p["model"].split(" (")[0]: p["frontier"] for p in f["points"]}
    # 싸고 똑똑(DeepSeek)·비싸지만 최고 지능(Closed-1)은 프런티어,
    # 더 싼 모델보다 지능이 낮은 Closed-2는 지배당해 탈락
    assert flags["DeepSeek-V3.2"] is True
    assert flags["Closed-1"] is True
    assert flags["Closed-2"] is False
    assert f["path"].startswith("M ")
    assert any(t["label"] == "$1" for t in f["ticks"])  # 로그 축 눈금


def test_agents_page_paper_link_frontier_and_board_link(conn):
    """paper-with-me 고유 기능: 모델→논문 직행, 가성비 프런티어,
    병합 리더보드 직행 링크."""
    from app import queries

    queries._agent_paper_cache.clear()
    # 기반 버전 테크 리포트만 존재 — 'DeepSeek-V3.2'가 점진 완화로 닿아야 함
    conn.execute(
        "INSERT INTO papers (paper_url, title, abstract) VALUES (?,?,?)",
        ("https://pwm.test/paper/deepseek-v3-tech-report",
         "DeepSeek-V3 Technical Report", "We present DeepSeek-V3."))
    _seed_value_frontier(conn)
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    r = TestClient(create_app(Path(db_file))).get("/agents")
    assert r.status_code == 200
    assert "가성비 프런티어" in r.text
    assert "frontier-scatter" in r.text
    assert "/paper/deepseek-v3-tech-report" in r.text        # 논문 직행
    assert "/sota/language-modelling/price-per-1m-tokens-blended-3-1" \
        in r.text                                            # 보드 직행
    queries._agent_paper_cache.clear()


def test_models_page_empty_state(tmp_path):
    db2 = tmp_path / "empty.sqlite"
    pwc_db.connect(db2).close()
    r = TestClient(create_app(db2)).get("/agents")
    assert r.status_code == 200
    assert "AA_API_KEY" in r.text  # 데이터 없을 때 설정 안내


def test_aa_raw_cost_not_percent_normalized(conn, monkeypatch):
    """비용(달러) 값은 0~1이어도 백분율로 변환하면 안 된다."""
    monkeypatch.setenv("AA_API_KEY", "k")
    payload = {"data": [{"name": "M", "release_date": "2026-01-01",
                         "evaluations": {
                             "cost_per_intelligence_index_task": 0.37}}]}

    def fake_urlopen(req, timeout=0):
        import io
        return io.BytesIO(json.dumps(payload).encode())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    external_boards.collect_artificial_analysis(conn)
    conn.commit()
    row = conn.execute(
        "SELECT metrics FROM sota_rows WHERE dataset="
        "'Cost per Intelligence Index Task'").fetchone()
    assert json.loads(row[0]) == {"Cost per task (USD)": "0.37"}
