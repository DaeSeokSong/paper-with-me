"""아카이브 덤프 다운로드."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import sources


def download_file(url: str, dest: Path) -> Path:
    """URL을 dest로 내려받는다. 이미 존재하면 건너뛴다."""
    if dest.exists():
        print(f"  건너뜀 (이미 존재): {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with sources.open_with_retry(url) as resp, open(part, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    part.rename(dest)
    print(f"  완료: {dest} ({dest.stat().st_size:,} bytes)")
    return dest


def download_all(data_dir: Path, only: list[str] | None = None) -> dict[str, Path]:
    """덤프 5종(또는 only로 지정한 것만)을 data_dir/raw/ 아래로 내려받는다.

    JSON 덤프는 단일 파일(raw/{name}.json[.gz])로, Parquet 샤드는 디렉터리
    (raw/{name}/)로 저장한다. 반환값: 논리 이름 -> 로컬 경로.
    """
    raw_dir = data_dir / "raw"
    results: dict[str, Path] = {}
    names = only or list(sources.DUMPS)
    for name in names:
        repo = sources.DUMPS[name]
        print(f"[{name}] {sources.ARCHIVE_ORG}/{repo}")
        try:
            files = sources.list_repo_files(repo)
            data_files = sources.pick_data_files(files)
        except Exception as e:  # noqa: BLE001 - 개별 덤프 실패는 보고 후 계속
            print(f"  실패: {e}", file=sys.stderr)
            continue
        print(f"  선택된 덤프 파일: {data_files}")
        if len(data_files) == 1 and not data_files[0].endswith(".parquet"):
            f = data_files[0]
            dest = raw_dir / f"{name}{_suffix_of(f)}"
            results[name] = download_file(sources.resolve_url(repo, f), dest)
        else:
            shard_dir = raw_dir / name
            for f in data_files:
                download_file(sources.resolve_url(repo, f), shard_dir / Path(f).name)
            results[name] = shard_dir
    return results


def _suffix_of(filename: str) -> str:
    return ".json.gz" if filename.endswith(".json.gz") else ".json"


def find_local_dump(data_dir: Path, name: str) -> Path | None:
    """이미 내려받은 덤프(단일 파일 또는 parquet 샤드 디렉터리)를 찾는다."""
    raw_dir = data_dir / "raw"
    for suffix in (".json.gz", ".json"):
        p = raw_dir / f"{name}{suffix}"
        if p.exists():
            return p
    shard_dir = raw_dir / name
    if shard_dir.is_dir() and any(shard_dir.glob("*.parquet")):
        return shard_dir
    return None
