"""아카이브 덤프 소스 정의.

원본 paperswithcode.com 공개 덤프의 마지막 스냅샷은 Hugging Face의
`pwc-archive` 조직에 보존되어 있다. 저장소 내 파일명은 하드코딩하지 않고
HF API로 런타임에 탐색한다.
"""

from __future__ import annotations

import json
import urllib.request

HF_HOST = "https://huggingface.co"
ARCHIVE_ORG = "pwc-archive"

# 논리 이름 -> pwc-archive 데이터셋 저장소 이름
DUMPS: dict[str, str] = {
    "papers": "papers-with-abstracts",
    "links": "links-between-paper-and-code",
    "evaluations": "evaluation-tables",
    "methods": "methods",
    "datasets": "datasets",
}

_USER_AGENT = "paper-with-me/0.1 (+https://github.com/DaeSeokSong/paper-with-me)"


def _get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def list_repo_files(repo_name: str) -> list[str]:
    """데이터셋 저장소의 최상위 파일 경로 목록을 반환한다."""
    url = f"{HF_HOST}/api/datasets/{ARCHIVE_ORG}/{repo_name}/tree/main"
    entries = _get_json(url)
    return [e["path"] for e in entries if e.get("type") == "file"]


def pick_data_file(files: list[str]) -> str:
    """저장소 파일 중 실제 덤프 파일을 고른다 (.json.gz 우선, 다음 .json)."""
    for suffix in (".json.gz", ".json"):
        candidates = [f for f in files if f.endswith(suffix)]
        if candidates:
            # 파일이 여럿이면 가장 큰 이름 기준이 아니라 사전순 첫 파일을 쓰되,
            # README 류는 확장자로 이미 걸러졌다.
            return sorted(candidates)[0]
    raise FileNotFoundError(
        f"JSON 덤프 파일을 찾지 못했습니다. 저장소 파일 목록: {files}"
    )


def resolve_url(repo_name: str, filename: str) -> str:
    return f"{HF_HOST}/datasets/{ARCHIVE_ORG}/{repo_name}/resolve/main/{filename}"
