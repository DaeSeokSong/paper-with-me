"""Hugging Face 배포 스크립트 (GitHub Actions에서 실행).

1. data/pwc.sqlite → HF Datasets 리포({user}/pwc-restore-data)에 업로드
2. 앱 소스 + Dockerfile → HF Docker Space({user}/paper-with-me)에 동기화
3. Space 환경변수(PWC_DATA_REPO) 설정 후 재시작

HF_TOKEN(write 권한)이 환경변수에 있어야 하며, 없으면 안내 후 정상 종료한다
(토큰 등록 전에 워크플로가 실패하지 않도록).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent

SPACE_README = """---
title: paper-with-me
emoji: 📄
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 8000
pinned: true
license: agpl-3.0
---

# paper-with-me

Papers with Code 복원 프로젝트 — 논문·코드·SOTA 리더보드 탐색 서비스.

- 소스 코드: https://github.com/DaeSeokSong/paper-with-me (AGPL-3.0)
- 데이터: 2025-07 아카이브 스냅샷 + 일일 갱신 (CC-BY-SA 4.0)
- 비공식 복원 프로젝트로, Meta 및 원 paperswithcode.com과 무관합니다.
"""


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN이 없어 배포를 건너뜁니다. GitHub 리포 Settings → "
              "Secrets and variables → Actions에 write 권한 HF 토큰을 "
              "HF_TOKEN으로 등록하세요.")
        return 0

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    user = api.whoami()["name"]
    data_repo = f"{user}/pwc-restore-data"
    space_repo = f"{user}/paper-with-me"

    db = ROOT / "data" / "pwc.sqlite"
    if db.exists():
        print(f"[deploy] 데이터 업로드 → {data_repo} ({db.stat().st_size:,} bytes)",
              flush=True)
        api.create_repo(data_repo, repo_type="dataset", exist_ok=True)
        api.upload_file(path_or_fileobj=str(db), path_in_repo="pwc.sqlite",
                        repo_id=data_repo, repo_type="dataset",
                        commit_message="Update snapshot")
    else:
        print("[deploy] data/pwc.sqlite 없음 — 데이터 업로드 건너뜀", flush=True)

    print(f"[deploy] Space 동기화 → {space_repo}", flush=True)
    api.create_repo(space_repo, repo_type="space", space_sdk="docker",
                    exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "README.md").write_text(SPACE_README, encoding="utf-8")
        api.upload_file(path_or_fileobj=str(Path(td) / "README.md"),
                        path_in_repo="README.md", repo_id=space_repo,
                        repo_type="space", commit_message="Update Space config")
    for path in ("Dockerfile", "LICENSE", "pyproject.toml"):
        api.upload_file(path_or_fileobj=str(ROOT / path), path_in_repo=path,
                        repo_id=space_repo, repo_type="space",
                        commit_message=f"Sync {path}")
    for folder in ("app", "pwc"):
        api.upload_folder(folder_path=str(ROOT / folder), path_in_repo=folder,
                          repo_id=space_repo, repo_type="space",
                          commit_message=f"Sync {folder}/",
                          ignore_patterns=["__pycache__/*", "*.pyc"])

    api.add_space_variable(space_repo, "PWC_DATA_REPO", data_repo)
    api.restart_space(space_repo)
    print(f"[deploy] 완료 — https://huggingface.co/spaces/{space_repo}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
