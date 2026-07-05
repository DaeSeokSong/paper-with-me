"""아카이브 덤프 소스 정의.

원본 paperswithcode.com 공개 덤프의 마지막 스냅샷은 Hugging Face의
`pwc-archive` 조직에 보존되어 있다. 저장소 내 파일명은 하드코딩하지 않고
HF API로 런타임에 탐색한다.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
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


# HF는 대용량 동시 다운로드에 간헐적으로 429/5xx를 준다 — 재시도 없이는
# 100분짜리 빌드가 일시 장애 하나로 통째로 죽는다 (run 28730365778)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRIES = 5


def open_with_retry(url: str, timeout: int = 120):
    """일시 오류(429/5xx/네트워크)에 지수 백오프로 재시도하는 urlopen."""
    for attempt in range(1, _RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            return urllib.request.urlopen(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if (code is not None and code not in _RETRY_STATUS) \
                    or attempt == _RETRIES:
                raise
            wait = 15 * attempt
            print(f"  일시 오류({e}) — {wait}s 후 재시도 ({attempt}/{_RETRIES - 1})",
                  file=sys.stderr)
            time.sleep(wait)


def _get_json(url: str) -> object:
    with open_with_retry(url, timeout=60) as resp:
        return json.load(resp)


def list_repo_files(repo_name: str) -> list[str]:
    """데이터셋 저장소의 전체 파일 경로 목록을 재귀적으로 반환한다.

    아카이브 저장소는 데이터 파일을 하위 폴더에 두는 경우가 있어
    recursive 조회가 필요하다.
    """
    url = f"{HF_HOST}/api/datasets/{ARCHIVE_ORG}/{repo_name}/tree/main?recursive=true"
    entries = _get_json(url)
    return [e["path"] for e in entries if e.get("type") == "file"]


def pick_data_files(files: list[str]) -> list[str]:
    """저장소 파일 중 실제 덤프 파일들을 고른다.

    우선순위: 원본 JSON 덤프(.json.gz > .json) 1개 → 없으면 HF가 변환한
    Parquet 샤드 전체(data/train-0000X-of-0000N.parquet).
    """
    for suffix in (".json.gz", ".json"):
        candidates = [f for f in files if f.endswith(suffix)]
        if candidates:
            return [sorted(candidates)[0]]
    shards = sorted(f for f in files if f.endswith(".parquet"))
    if shards:
        return shards
    raise FileNotFoundError(
        f"덤프 파일(.json/.json.gz/.parquet)을 찾지 못했습니다. "
        f"저장소 파일 목록: {files}"
    )


def resolve_url(repo_name: str, filename: str) -> str:
    return f"{HF_HOST}/datasets/{ARCHIVE_ORG}/{repo_name}/resolve/main/{filename}"
