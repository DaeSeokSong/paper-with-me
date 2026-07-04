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
    "We propose WeirdNet for image classification. "
    "WeirdNet achieves 12.0% accuracy on CIFAR-100."
)
ABSTRACT_NO_BENCH = "We study the theory of optimization landscapes."
# task 언급 없음 — 데이터셋 살포 방지 게이트 검증 (실데이터 1차 가동에서
# 발견: "CIFAR-10" 언급 하나로 무관한 task 보드 10곳에 추가됨)
ABSTRACT_NO_TASK = (
    "We propose SprayNet, a compression method. "
    "SprayNet achieves 95.00% on CIFAR-100."
)
# %-없는 수치는 배속·계수 오인 방지를 위해 무시 ("1.31x speedup" 등)
ABSTRACT_NO_PERCENT = (
    "We propose SpeedNet for image classification with a 1.31 speedup "
    "on CIFAR-100 and accuracy of 95.5 without percent sign."
)


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
    assert results_extract.collect(conn) == 0  # 재실행도 0 (stateless)


def test_rejects_unmentioned_task_and_bare_numbers(conn):
    _add_paper(conn, "spray-paper", ABSTRACT_NO_TASK)
    _add_paper(conn, "speed-paper", ABSTRACT_NO_PERCENT)
    assert results_extract.collect(conn) == 0


def test_reextraction_purges_stale_auto_rows(conn):
    """규칙 강화 이전 실행이 남긴 오염 auto 행은 다음 실행에서 정화된다."""
    conn.execute(
        "INSERT INTO sota_rows (task,dataset,model_name,metrics,paper_url,"
        "code_links,source) VALUES (?,?,?,?,?,?,?)",
        ("Image Classification", "CIFAR-100", "JunkNet",
         json.dumps({"Percentage correct": "1.31"}),
         "https://paperswithcode.com/paper/junk", "[]", "auto"),
    )
    conn.commit()
    results_extract.collect(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sota_rows WHERE source='auto'"
    ).fetchone()[0] == 0


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
