"""커뮤니티 리더보드 기여 검증·적재 테스트."""

import json
import sqlite3
from pathlib import Path

import pytest

from pwc import contrib, db

VALID = {
    "task": "Image Classification",
    "dataset": "CIFAR-100",
    "model_name": "MyNet-XL",
    "metrics": {"Percentage correct": "96.90"},
    "paper_url": "https://arxiv.org/abs/2507.12345",
    "paper_date": "2026-07-01",
    "code_links": [{"title": "me/mynet", "url": "https://github.com/me/mynet"}],
}


def test_repo_contributions_dir_is_valid():
    """리포에 커밋된 모든 기여 파일이 스키마를 통과해야 한다 (PR 게이트)."""
    directory = Path(__file__).parent.parent / "contributions"
    _, errors = contrib.load_contributions(directory)
    assert errors == [], errors


def test_validate_rejects_bad_records(tmp_path):
    bad = [
        dict(VALID, task=""),                          # 필수 누락
        dict(VALID, metrics={}),                       # 빈 metrics
        dict(VALID, paper_date="07/01/2026"),          # 날짜 형식
        dict(VALID, paper_url="ftp://x"),              # URL 스킴
        dict(VALID, code_links=[{"title": "no-url"}]),  # 링크 url 누락
    ]
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    _, errors = contrib.load_contributions(tmp_path)
    assert len(errors) == 5


def test_ingest_is_idempotent(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "mynet.json").write_text(json.dumps(VALID), encoding="utf-8")
    conn = db.connect(tmp_path / "pwc.sqlite")
    conn.row_factory = sqlite3.Row
    assert contrib.ingest_contributions(conn, tmp_path / "c") == 1
    assert contrib.ingest_contributions(conn, tmp_path / "c") == 0  # 중복 스킵
    row = conn.execute(
        "SELECT metrics FROM sota_rows WHERE model_name='MyNet-XL'"
    ).fetchone()
    assert json.loads(row["metrics"]) == {"Percentage correct": "96.90"}
    conn.close()


def test_ingest_raises_on_invalid_dir(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "broken.json").write_text("{not json", encoding="utf-8")
    conn = db.connect(tmp_path / "pwc.sqlite")
    with pytest.raises(ValueError, match="검증 실패"):
        contrib.ingest_contributions(conn, tmp_path / "c")
    conn.close()
