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


class FakeRuntimeApi:
    """restart_for_data 가드용 — 빌드 중 restart는 빌드를 재큐잉해
    RUNNING_BUILDING이 끝나지 않는 실사고가 있었다."""

    def __init__(self, stage):
        self.stage = stage
        self.calls: list[str] = []

    def get_space_runtime(self, repo_id):
        if isinstance(self.stage, Exception):
            raise self.stage

        class _RT:
            pass

        rt = _RT()
        rt.stage = self.stage
        return rt

    def restart_space(self, repo_id, factory_reboot=False):
        self.calls.append(
            "factory_reboot" if factory_reboot else "restart_space")


def test_restart_skipped_while_building():
    for stage in ("BUILDING", "RUNNING_BUILDING", "APP_STARTING"):
        api = FakeRuntimeApi(stage)
        deploy_hf.restart_for_data(api, "u/space")
        assert api.calls == [], stage


def test_restart_runs_when_running():
    api = FakeRuntimeApi("RUNNING")
    deploy_hf.restart_for_data(api, "u/space")
    assert api.calls == ["restart_space"]


def test_restart_runs_when_stage_probe_fails():
    api = FakeRuntimeApi(RuntimeError("api down"))
    deploy_hf.restart_for_data(api, "u/space")
    assert api.calls == ["restart_space"]  # 조회 실패 시 기존 동작 유지


def test_error_stage_triggers_factory_reboot():
    """일반 restart는 실패한 기존 이미지를 재사용한다 — BUILD_ERROR에서는
    이미지를 새로 빌드하는 factory reboot여야 복구된다."""
    for stage in ("BUILD_ERROR", "RUNTIME_ERROR"):
        api = FakeRuntimeApi(stage)
        deploy_hf.restart_for_data(api, "u/space")
        assert api.calls == ["factory_reboot"], stage
