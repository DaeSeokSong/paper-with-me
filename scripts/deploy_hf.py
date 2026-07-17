"""Hugging Face 배포 스크립트 (GitHub Actions에서 실행).

1. data/pwc.sqlite → HF Datasets 리포({user}/pwc-restore-data)에 업로드
2. 앱 소스 + Dockerfile → HF Docker Space({user}/paper-with-me)에 **단일
   커밋으로** 동기화 (파일별 커밋은 커밋마다 재빌드를 유발하고, 중간 실패
   시 혼합 버전이 배포된다)
3. Space 환경변수(PWC_DATA_REPO) 설정 후 재시작 — 데이터가 갱신됐다면
   코드 동기화가 실패해도 재시작은 보장한다

HF_TOKEN(write 권한)이 환경변수에 있어야 하며, 없으면 안내 후 정상 종료한다
(토큰 등록 전에 워크플로가 실패하지 않도록).
"""

from __future__ import annotations

import os
import shutil
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


def upload_data(api, data_repo: str) -> bool:
    db = ROOT / "data" / "pwc.sqlite"
    if not db.exists():
        print("[deploy] data/pwc.sqlite 없음 — 데이터 업로드 건너뜀", flush=True)
        return False
    print(f"[deploy] 데이터 업로드 → {data_repo} ({db.stat().st_size:,} bytes)",
          flush=True)
    if not api.repo_exists(data_repo, repo_type="dataset"):
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
    # 매일 13GB 커밋이 쌓이면 LFS 히스토리가 무한 팽창한다 — 이력을 접는다
    try:
        api.super_squash_history(repo_id=data_repo, repo_type="dataset")
        print("[deploy] 데이터 리포 히스토리 정리 완료", flush=True)
    except Exception as e:  # noqa: BLE001 - 정리 실패는 비치명
        print(f"[deploy] 히스토리 정리 건너뜀: {e}", flush=True)
    return True


def sync_space(api, space_repo: str, data_repo: str) -> None:
    print(f"[deploy] Space 동기화 → {space_repo} (단일 커밋)", flush=True)
    # HF가 2026-07-08부터 무료 계정의 Docker Space '생성'을 402로 거부한다.
    # exist_ok=True여도 생성 API를 먼저 때리므로, 이미 존재하는 Space는
    # 생성 호출 자체를 건너뛰어야 기존 Space 운영이 계속된다.
    if api.repo_exists(space_repo, repo_type="space"):
        print("[deploy] Space 이미 존재 — 생성 건너뜀", flush=True)
    else:
        api.create_repo(space_repo, repo_type="space", space_sdk="docker",
                        exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        (staging / "README.md").write_text(SPACE_README, encoding="utf-8")
        for f in ("Dockerfile", "LICENSE", "pyproject.toml"):
            shutil.copy(ROOT / f, staging / f)
        for folder in ("app", "pwc"):
            shutil.copytree(ROOT / folder, staging / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        api.upload_folder(folder_path=str(staging), path_in_repo="",
                          repo_id=space_repo, repo_type="space",
                          commit_message="Sync app from GitHub",
                          delete_patterns=["app/**", "pwc/**"])
    api.add_space_variable(space_repo, "PWC_DATA_REPO", data_repo)


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN이 없어 배포를 건너뜁니다. GitHub 리포 Settings → "
              "Secrets and variables → Actions에 write 권한 HF 토큰을 "
              "HF_TOKEN으로 등록하세요.")
        return 0

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    try:
        user = api.whoami()["name"]
    except Exception as e:  # noqa: BLE001
        print(f"[deploy] HF 토큰 인증 실패 — 만료/철회 여부를 확인하고 "
              f"HF_TOKEN 시크릿을 재발급하세요: {e}", file=sys.stderr)
        return 1
    data_repo = f"{user}/pwc-restore-data"
    space_repo = f"{user}/paper-with-me"

    data_updated = upload_data(api, data_repo)
    try:
        sync_space(api, space_repo, data_repo)
    finally:
        # 코드 동기화가 실패해도, 데이터가 갱신됐다면 재시작으로 새 스냅샷을
        # 반영한다 (bootstrap이 시작 시 최신 revision을 내려받는다)
        if data_updated:
            restart_for_data(api, space_repo)

    if not wait_for_space(api, space_repo):
        return 1
    print(f"[deploy] 배포 검증 완료 — https://huggingface.co/spaces/{space_repo}",
          flush=True)
    return 0


# 빌드/기동이 이미 진행 중인 단계 — 이때의 restart는 진행 중인 빌드를
# 대기열 맨 뒤로 되돌려, 연속 배포 시 빌드가 영영 끝나지 않는다
_IN_PROGRESS_STAGES = {"BUILDING", "RUNNING_BUILDING", "APP_STARTING"}


def restart_for_data(api, space_repo: str) -> None:
    """새 데이터 스냅샷 반영용 재시작 — 빌드/기동 중이면 생략한다.

    무료 티어에서 Space 빌드가 수십 분 걸리는 동안 다음 배포가
    restart_space를 호출하면 빌드가 재큐잉돼 RUNNING_BUILDING이 1시간 넘게
    이어지는 실사고가 있었다. 진행 중인 빌드가 끝나면 bootstrap이 시작
    시점에 최신 스냅샷을 내려받으므로 재시작 없이도 데이터는 반영된다.
    """
    try:
        stage = str(getattr(api.get_space_runtime(space_repo), "stage", None))
    except Exception as e:  # noqa: BLE001 - 조회 실패 시 기존 동작(재시작) 유지
        stage = f"조회 실패: {e}"
    if stage in _IN_PROGRESS_STAGES:
        print(f"[deploy] 빌드/기동 진행 중({stage}) — 재시작 생략", flush=True)
        return
    api.restart_space(space_repo)
    print("[deploy] Space 재시작 요청 완료", flush=True)


def wait_for_space(api, space_repo: str, timeout: int = 5100) -> bool:
    """Space가 빌드→기동을 마치고 실제 HTTP 200을 돌려줄 때까지 대기한다.

    무료 티어 빌드 대기열 지연으로 30분(1800s)이 모자라 배포가 연속
    타임아웃된 적이 있다 — 워크플로 잡 한도(120분) 안에서 85분까지 기다린다.
    """
    user, name = space_repo.split("/")
    url = f"https://{user.lower()}-{name.lower()}.hf.space/"
    start = time.time()
    deadline = start + timeout
    probe_errors = 0
    while time.time() < deadline:
        try:
            stage = getattr(api.get_space_runtime(space_repo), "stage", None)
            probe_errors = 0
        except Exception as e:  # noqa: BLE001
            probe_errors += 1
            stage = f"조회 실패({probe_errors}): {e}"
            if probe_errors >= 10:
                print("[deploy] Space 상태 조회가 계속 실패합니다", flush=True)
                return False
        print(f"[deploy] Space 상태: {stage} "
              f"(경과 {int(time.time() - start) // 60}분)", flush=True)
        if str(stage) in ("BUILD_ERROR", "RUNTIME_ERROR", "STOPPED", "PAUSED"):
            print("[deploy] Space가 실패 상태입니다. Space 로그를 확인하세요.")
            return False
        if str(stage) == "RUNNING":
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    body = resp.read(4096).decode(errors="replace")
                if resp.status == 200 and "paper-with-me" in body:
                    print(f"[deploy] 서비스 응답 OK: {url}", flush=True)
                    return live_check(url)
            except Exception as e:  # noqa: BLE001 - 기동 직후 일시 오류 허용
                print(f"[deploy] 응답 대기 중: {e}", flush=True)
        time.sleep(30)
    print("[deploy] 대기 시간 초과")
    return False


def live_check(base: str) -> bool:
    """배포 직후 라이브에서 핵심 사용자 동선을 확인한다.

    사용자가 실제로 겪은 장애(리더보드 → 논문 링크 404)가 배포 게이트를
    통과한 적이 있어, 홈 200 확인만으로는 부족하다 — 라이브 리더보드의
    ground truth 수치와, 렌더링된 논문 링크 전수가 열리는지 본다.
    """
    import re

    # ?per=100 — 표 페이지네이션과 무관하게 상위 100행의 링크를 점검
    board_url = (base.rstrip("/")
                 + "/sota/image-classification/cifar-100?per=100")
    try:
        with urllib.request.urlopen(board_url, timeout=60) as resp:
            html = resp.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[deploy] 라이브 리더보드 조회 실패: {e}")
        return False
    if "96.08" not in html:
        print("[deploy] 라이브 리더보드에 ground truth(96.08)가 없습니다")
        return False
    # 재빌드 스냅샷 배포 확인용 진단 — 값이 있는 스냅샷에서만 컬럼이 뜬다
    print("[deploy] 라이브 Extra Training Data 컬럼: "
          + ("표시됨" if "Extra Training Data" in html else "없음(재빌드 전 스냅샷)"),
          flush=True)
    broken = []
    for href in sorted(set(re.findall(r'href="(/paper/[^"]+)"', html))):
        last_err = None
        for _attempt in range(2):  # 일시 네트워크 오류로 배포가 실패하지 않게
            try:
                with urllib.request.urlopen(base.rstrip("/") + href,
                                            timeout=60) as resp:
                    last_err = (None if resp.status == 200
                                else f"{href} → {resp.status}")
                break
            except Exception as e:  # noqa: BLE001 - HTTPError(404 등) 포함
                last_err = f"{href} → {e}"
                time.sleep(3)
        if last_err:
            broken.append(last_err)
    if broken:
        print(f"[deploy] 라이브 논문 링크 {len(broken)}건 깨짐:")
        for b in broken:
            print(f"  {b}")
        return False
    # 리더보드 ↔ 데이터셋 카탈로그 왕복 (QA에서 복원한 동선) 라이브 확인
    for href in set(re.findall(r'href="(/dataset/[^"]+)"', html)):
        try:
            with urllib.request.urlopen(base.rstrip("/") + href,
                                        timeout=60) as resp:
                if resp.status != 200:
                    print(f"[deploy] 데이터셋 링크 깨짐: {href} → {resp.status}")
                    return False
        except Exception as e:  # noqa: BLE001
            print(f"[deploy] 데이터셋 링크 깨짐: {href} → {e}")
            return False
    print("[deploy] 라이브 점검 OK: 리더보드 수치·논문 링크 전수 연결")
    return True


if __name__ == "__main__":
    sys.exit(main())
