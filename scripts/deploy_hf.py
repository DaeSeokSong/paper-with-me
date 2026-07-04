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
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent

DATA_README = """---
license: cc-by-sa-4.0
---

# pwc-restore-data

[paper-with-me](https://github.com/DaeSeokSong/paper-with-me) 서비스용 SQLite 스냅샷.
Papers with Code 2025-07 아카이브([pwc-archive](https://huggingface.co/pwc-archive),
CC-BY-SA 4.0) + arXiv/HF Daily Papers/GitHub 일일 수집분.
"""

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
        with tempfile.TemporaryDirectory() as td:
            readme = Path(td) / "README.md"
            readme.write_text(DATA_README, encoding="utf-8")
            api.upload_file(path_or_fileobj=str(readme), path_in_repo="README.md",
                            repo_id=data_repo, repo_type="dataset",
                            commit_message="Update dataset card")
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
    print(f"[deploy] Space 재시작 — https://huggingface.co/spaces/{space_repo}",
          flush=True)

    if not wait_for_space(api, space_repo):
        return 1
    print(f"[deploy] 배포 검증 완료 — https://huggingface.co/spaces/{space_repo}",
          flush=True)
    return 0


def wait_for_space(api, space_repo: str, timeout: int = 1800) -> bool:
    """Space가 빌드→기동을 마치고 실제 HTTP 200을 돌려줄 때까지 대기한다."""
    user, name = space_repo.split("/")
    url = f"https://{user.lower()}-{name.lower()}.hf.space/"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            stage = getattr(api.get_space_runtime(space_repo), "stage", None)
        except Exception as e:  # noqa: BLE001
            stage = f"조회 실패: {e}"
        print(f"[deploy] Space 상태: {stage}", flush=True)
        if str(stage) in ("BUILD_ERROR", "RUNTIME_ERROR", "STOPPED", "PAUSED"):
            print("[deploy] Space가 실패 상태입니다. Space 로그를 확인하세요.")
            return False
        if str(stage) == "RUNNING":
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    body = resp.read(4096).decode(errors="replace")
                if resp.status == 200 and "paper-with-me" in body:
                    print(f"[deploy] 서비스 응답 OK: {url}", flush=True)
                    return True
            except Exception as e:  # noqa: BLE001 - 기동 직후 일시 오류 허용
                print(f"[deploy] 응답 대기 중: {e}", flush=True)
        time.sleep(30)
    print("[deploy] 대기 시간 초과")
    return False


if __name__ == "__main__":
    sys.exit(main())
