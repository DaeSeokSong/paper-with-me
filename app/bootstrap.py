"""배포 환경 부트스트랩 — HF Datasets의 스냅샷을 최신 상태로 준비한다.

HF Space 컨테이너 시작 시 실행된다. 데이터셋 리포의 최신 revision을 기록해
두고 비교하므로, persistent storage처럼 /data가 유지되는 환경에서도 스냅샷
갱신이 반영된다 (단순 존재 확인만 하면 첫날 데이터에 영구 고정된다).

사용법: python -m app.bootstrap && uvicorn app.main:app ...
환경변수: PWC_DB(기본 data/pwc.sqlite), PWC_DATA_REPO,
          PWC_FORCE_REFRESH=1 (무조건 재다운로드)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _remote_revision(repo: str) -> str | None:
    try:
        from huggingface_hub import HfApi
        return HfApi().dataset_info(repo).sha
    except Exception as e:  # noqa: BLE001 - 조회 실패 시 로컬 DB로 기동
        print(f"[bootstrap] revision 조회 실패 (로컬 DB 유지): {e}", flush=True)
        return None


def ensure_db() -> Path:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    repo = os.environ.get("PWC_DATA_REPO")
    marker = db_path.with_suffix(".revision")

    if db_path.exists() and not repo:
        print(f"[bootstrap] DB 존재 (PWC_DATA_REPO 미설정): {db_path}", flush=True)
        return db_path
    if not db_path.exists() and not repo:
        sys.exit(f"[bootstrap] {db_path}가 없고 PWC_DATA_REPO도 설정되지 않았습니다")

    remote = _remote_revision(repo)
    local = marker.read_text().strip() if marker.exists() else None
    force = os.environ.get("PWC_FORCE_REFRESH") == "1"
    if db_path.exists() and not force and (remote is None or remote == local):
        print(f"[bootstrap] DB 최신 상태 (revision {local}): {db_path}", flush=True)
        return db_path

    from huggingface_hub import hf_hub_download

    print(f"[bootstrap] {repo}에서 스냅샷 다운로드 중... "
          f"(로컬 {local} → 원격 {remote})", flush=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    downloaded = hf_hub_download(
        repo_id=repo, filename="pwc.sqlite", repo_type="dataset",
        local_dir=db_path.parent, revision=remote,
    )
    if Path(downloaded) != db_path:
        Path(downloaded).rename(db_path)
    if remote:
        marker.write_text(remote)
    print(f"[bootstrap] 완료: {db_path} ({db_path.stat().st_size:,} bytes)", flush=True)
    return db_path


if __name__ == "__main__":
    ensure_db()
