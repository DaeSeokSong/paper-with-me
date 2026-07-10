"""deploy_hf의 Space 생성 가드 — HF가 무료 계정의 Docker Space '생성'을
402로 거부하기 시작(2026-07-08)해도, 이미 존재하는 Space의 코드 동기화는
계속되어야 한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import deploy_hf  # noqa: E402


class FakeApi:
    def __init__(self, exists: bool):
        self.exists = exists
        self.calls: list[str] = []

    def repo_exists(self, repo_id, repo_type=None):
        self.calls.append(f"repo_exists:{repo_type}")
        return self.exists

    def create_repo(self, *a, **k):
        self.calls.append("create_repo")

    def upload_folder(self, **k):
        self.calls.append("upload_folder")

    def add_space_variable(self, *a, **k):
        self.calls.append("add_space_variable")


def test_sync_space_skips_creation_when_space_exists(tmp_path, monkeypatch):
    api = FakeApi(exists=True)
    deploy_hf.sync_space(api, "u/space", "u/data")
    assert "create_repo" not in api.calls  # 402를 유발하는 생성 호출 없음
    assert "upload_folder" in api.calls


def test_sync_space_creates_when_missing():
    api = FakeApi(exists=False)
    deploy_hf.sync_space(api, "u/space", "u/data")
    assert "create_repo" in api.calls
