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
                     "aime": 0.92, "unknown_bench": 0.5}},
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
    assert added == 4  # 모델1: mmlu_pro/gpqa/aime, 모델2: mmlu_pro
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
    assert json.loads(rows[3][3]) == {"Accuracy": "55.2"}


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
