import pytest

from app import bootstrap


def test_ensure_db_noop_when_exists(tmp_path, monkeypatch):
    db = tmp_path / "pwc.sqlite"
    db.write_bytes(b"stub")
    monkeypatch.setenv("PWC_DB", str(db))
    assert bootstrap.ensure_db() == db


def test_ensure_db_exits_without_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PWC_DB", str(tmp_path / "missing.sqlite"))
    monkeypatch.delenv("PWC_DATA_REPO", raising=False)
    with pytest.raises(SystemExit):
        bootstrap.ensure_db()
