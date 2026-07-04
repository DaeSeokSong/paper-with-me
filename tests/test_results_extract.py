"""초록 기반 리더보드 결과 자동 추출 (Phase 2 — 리더보드 갱신 자동화)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from pwc import db as pwc_db
from pwc.collectors import results_extract

ABSTRACT_HIT = (
    "We propose SuperNet, a novel architecture for image classification. "
    "SuperNet achieves 97.30% top-1 accuracy on CIFAR-100, surpassing the "
    "previous state of the art."
)
ABSTRACT_MISS_RANGE = (
    "We propose WeirdNet. WeirdNet achieves 12.0% accuracy on CIFAR-100."
)
ABSTRACT_NO_BENCH = "We study the theory of optimization landscapes."


@pytest.fixture()
def conn(tmp_path):
    c = pwc_db.connect(tmp_path / "extract.sqlite")
    # 기존 아카이브 보드 (sanity 범위의 기준)
    for model, acc in [("EffNet-L2 (SAM)", "96.08"), ("ViT-B", "94.20"),
                       ("ResNet", "91.00")]:
        c.execute(
            "INSERT INTO sota_rows (task,parent_task,dataset,model_name,"
            "metrics,paper_url,paper_title,paper_date,code_links) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Image Classification", None, "CIFAR-100", model,
             json.dumps({"Percentage correct": acc}),
             f"https://paperswithcode.com/paper/{model.lower()}", model,
             "2020-01-01", "[]"),
        )
    c.commit()
    yield c
    c.close()


def _add_paper(c, slug, abstract, date="2026-07-01"):
    c.execute(
        "INSERT INTO papers (paper_url,title,abstract,date,source) "
        "VALUES (?,?,?,?,'arxiv')",
        (f"https://paperswithcode.com/paper/{slug}",
         slug.replace("-", " ").title(), abstract, date),
    )


def test_extracts_result_within_known_range(conn):
    _add_paper(conn, "supernet-paper", ABSTRACT_HIT)
    added = results_extract.collect(conn)
    assert added == 1
    row = conn.execute(
        "SELECT * FROM sota_rows WHERE source='auto'").fetchone()
    row = dict(zip([d[0] for d in conn.execute(
        "SELECT * FROM sota_rows LIMIT 0").description], row))
    assert row["task"] == "Image Classification"
    assert row["dataset"] == "CIFAR-100"
    assert row["model_name"] == "SuperNet"  # "We propose SuperNet" 추출
    assert json.loads(row["metrics"]) == {"Percentage correct": "97.30"}


def test_rejects_out_of_range_and_no_benchmark(conn):
    _add_paper(conn, "weird-paper", ABSTRACT_MISS_RANGE)
    _add_paper(conn, "theory-paper", ABSTRACT_NO_BENCH)
    assert results_extract.collect(conn) == 0
    # 시도한 논문은 로그에 남아 재시도하지 않는다 (멱등)
    assert conn.execute(
        "SELECT COUNT(*) FROM result_extract_log").fetchone()[0] == 2
    assert results_extract.collect(conn) == 0


def test_auto_rows_do_not_feed_sanity_baseline(conn):
    """auto 행이 다음 추출의 기준이 되면 오염이 누적 표류한다 — 인덱스는
    archive/contrib 행만 사용."""
    _add_paper(conn, "supernet-paper", ABSTRACT_HIT)
    results_extract.collect(conn)
    index = results_extract._benchmark_index(conn)
    values = index["cifar-100"][0]["metrics"]["Percentage correct"]
    assert 97.30 not in values


def test_board_merges_auto_row_by_value_with_badge(conn, tmp_path):
    """자동 추출 행은 주 지표 값 순서에 맞는 자리에 끼어들고(97.3 > 96.08
    → 1위), '자동 추출' 배지가 붙는다."""
    _add_paper(conn, "supernet-paper", ABSTRACT_HIT)
    results_extract.collect(conn)
    conn.commit()
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    from pathlib import Path
    client = TestClient(create_app(Path(db_file)))
    r = client.get("/sota/image-classification/cifar-100")
    assert r.status_code == 200
    assert "자동 추출" in r.text
    # 병합 순서: SuperNet(97.30)이 1위 금메달
    import re
    first_model = re.search(r"<b>([^<]+)</b>", r.text).group(1)
    assert first_model == "SuperNet"
    assert 'class="rank-gold"' in r.text
