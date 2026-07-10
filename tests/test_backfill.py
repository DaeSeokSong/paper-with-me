"""아카이브 이후 공백 기간 백필 — '2025-08~2026-06 논문이 하나도 없어
보이는' 수집 공백(사용자 보고)의 회귀 테스트."""

import json

import pytest

from pwc import db as pwc_db
from pwc.collectors import arxiv


@pytest.fixture()
def conn(tmp_path):
    c = pwc_db.connect(tmp_path / "bf.sqlite")
    yield c
    c.close()


def _paper(i, date):
    return {"arxiv_id": f"2509.{i:05d}", "title": f"Backfilled Paper {i}",
            "abstract": "We study things.", "authors": ["Kim"],
            "date": date, "updated": date,
            "url_abs": f"https://arxiv.org/abs/2509.{i:05d}",
            "url_pdf": f"https://arxiv.org/pdf/2509.{i:05d}"}


def test_backfill_iterates_windows_and_upserts(conn, monkeypatch):
    windows = []

    def fake_window(start, end, page_size=1000, delay=3.0):
        windows.append((start, end))
        base = len(windows) * 10
        yield [_paper(base + j, f"{start[:4]}-{start[4:6]}-{start[6:8]}")
               for j in range(3)]

    monkeypatch.setattr(arxiv, "fetch_window", fake_window)
    added = arxiv.backfill(conn, "2025-07-01", "2025-08-09", window_days=14)
    # 40일 → 14일 창 3개, 창마다 3편
    assert len(windows) == 3
    assert windows[0][0] == "202507010000"
    assert windows[-1][1] == "202508092359"
    assert added == 9
    assert conn.execute(
        "SELECT COUNT(*) FROM papers WHERE source='arxiv'"
    ).fetchone()[0] == 9


def test_backfill_is_idempotent(conn, monkeypatch):
    def fake_window(start, end, page_size=1000, delay=3.0):
        yield [_paper(1, "2025-09-01")]

    monkeypatch.setattr(arxiv, "fetch_window", fake_window)
    assert arxiv.backfill(conn, "2025-09-01", "2025-09-01") == 1
    assert arxiv.backfill(conn, "2025-09-01", "2025-09-01") == 0  # 재실행
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_fetch_window_paginates_until_short_page(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if len(calls) == 1:
            return [_paper(i, "2025-09-01") for i in range(2)]
        return [_paper(99, "2025-09-01")]  # 페이지 크기 미만 → 종료

    monkeypatch.setattr(arxiv, "_fetch", fake_fetch)
    monkeypatch.setattr(arxiv.time, "sleep", lambda s: None)
    pages = list(arxiv.fetch_window("202509010000", "202509142359",
                                    page_size=2))
    assert [len(p) for p in pages] == [2, 1]
    assert "start=0" in calls[0] and "start=2" in calls[1]
    assert "submittedDate" in calls[0]


def test_backfill_cli_registered():
    from pwc import cli
    # argparse 등록 확인 — 인자 파싱만 (네트워크 없음)
    parser_error = None
    try:
        cli.main(["backfill", "--start", "2025-07-01", "--end",
                  "2025-07-01", "--data-dir", "/nonexistent-dir-x/y"])
    except SystemExit:
        parser_error = True
    except Exception:
        pass  # DB 경로 오류 등 — 파싱은 통과했다는 뜻
    assert parser_error is not True


def test_auto_tag_combined_matcher_semantics():
    """결합 정규식 최적화 후에도 태깅 의미 보존 — 구체명 우선, 부분
    문자열 제외, 상한 4개."""
    from pwc.collectors import auto_tag
    vocab = sorted(["Image Classification", "Few-Shot Image Classification",
                    "Object Detection"], key=len, reverse=True)
    tags = auto_tag.tag_text(
        "Prototype Networks for Few-Shot Image Classification",
        "We study few-shot image classification and object detection.",
        vocab)
    assert "Few-Shot Image Classification" in tags
    assert "Object Detection" in tags
    assert "Image Classification" not in tags
