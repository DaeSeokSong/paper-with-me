"""배포 환경 부트스트랩 — DB가 없으면 HF Datasets에서 스냅샷을 내려받는다.

HF Space 컨테이너는 무상태(ephemeral)라 시작 시 데이터를 채워야 한다.
사용법: python -m app.bootstrap && uvicorn app.main:app ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_db() -> Path:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    if db_path.exists():
        print(f"[bootstrap] DB 존재: {db_path}", flush=True)
        return db_path
    repo = os.environ.get("PWC_DATA_REPO")
    if not repo:
        sys.exit(f"[bootstrap] {db_path}가 없고 PWC_DATA_REPO도 설정되지 않았습니다")
    from huggingface_hub import hf_hub_download

    print(f"[bootstrap] {repo}에서 스냅샷 다운로드 중...", flush=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = hf_hub_download(
        repo_id=repo, filename="pwc.sqlite", repo_type="dataset",
        local_dir=db_path.parent,
    )
    if Path(downloaded) != db_path:
        Path(downloaded).rename(db_path)
    print(f"[bootstrap] 완료: {db_path} ({db_path.stat().st_size:,} bytes)", flush=True)
    return db_path


if __name__ == "__main__":
    ensure_db()
