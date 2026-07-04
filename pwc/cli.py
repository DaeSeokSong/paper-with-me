"""CLI: python -m pwc {download,ingest,build,stats}"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import db, download, ingest, sources


def _collect_local_dumps(data_dir: Path, only: list[str] | None) -> dict[str, Path]:
    dumps: dict[str, Path] = {}
    for name in only or list(sources.DUMPS):
        path = download.find_local_dump(data_dir, name)
        if path is None:
            print(f"[{name}] 덤프 파일이 없습니다. 먼저 `python -m pwc download`를 실행하세요.",
                  file=sys.stderr)
        else:
            dumps[name] = path
    return dumps


def cmd_download(args: argparse.Namespace) -> int:
    results = download.download_all(args.data_dir, args.only)
    return 0 if results else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    dumps = _collect_local_dumps(args.data_dir, args.only)
    if not dumps:
        return 1
    conn = db.connect(args.data_dir / "pwc.sqlite")
    ingest.ingest_all(conn, dumps)
    conn.close()
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """덤프별로 다운로드 → 적재 → 원본 삭제를 순차 수행한다.

    전체 덤프와 DB를 동시에 디스크에 두지 않아 CI 러너(디스크 ~14GB)에서도
    안전하게 빌드된다.
    """
    conn = db.connect(args.data_dir / "pwc.sqlite")
    # 대량 적재 전용 설정 (앱에서는 db.connect 기본값 사용)
    conn.execute("PRAGMA synchronous=OFF")
    ingested = 0
    for name in args.only or list(sources.DUMPS):
        results = download.download_all(args.data_dir, [name])
        path = results.get(name)
        if path is None:
            continue
        print(f"[{name}] {path} 적재 중...", flush=True)
        rows = ingest.INGESTERS[name](conn, path)
        print(f"  {rows:,} rows", flush=True)
        ingested += 1
        _remove(path)
    if ingested:
        print("FTS 인덱스 재구축 중...", flush=True)
        db.rebuild_fts(conn)
    conn.close()
    return 0 if ingested else 1


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def cmd_collect(args: argparse.Namespace) -> int:
    """Phase 2 수집기 실행 — 아카이브 스냅샷 이후의 신규 데이터를 채운다."""
    import os

    from .collectors import arxiv, github_links, hf_models, hf_papers

    conn = db.connect(args.data_dir / "pwc.sqlite")
    failures = 0
    for source in args.source:
        try:
            if source == "arxiv":
                arxiv.collect(conn, max_results=args.max_results)
            elif source == "hf":
                hf_papers.collect(conn)
            elif source == "hf-models":
                hf_models.collect(conn, max_papers=args.max_papers)
            elif source == "github":
                github_links.collect(conn, token=os.environ.get("GITHUB_TOKEN"),
                                     max_papers=args.max_papers)
        except Exception as e:  # noqa: BLE001 - 소스별 독립 실행
            print(f"[{source}] 수집 실패: {e}", file=sys.stderr)
            failures += 1
    # 트리거 도입 전 스냅샷에서 수집된 논문의 검색 인덱스 복구 (멱등)
    synced = db.sync_fts(conn)
    if synced:
        print(f"FTS 증분 동기화: {synced:,}편", flush=True)
    conn.close()
    return 1 if failures == len(args.source) else 0


def cmd_stats(args: argparse.Namespace) -> int:
    db_path = args.data_dir / "pwc.sqlite"
    if not db_path.exists():
        print(f"DB가 없습니다: {db_path}", file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    for table in ("papers", "repos", "datasets", "methods", "sota_rows", "signals"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:>10}: {n:,}")
    fresh = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE source != 'archive'"
    ).fetchone()[0]
    print(f"{'신규 논문':>9}: {fresh:,} (아카이브 이후 수집분)")
    print(f"{'fts':>10}: {'사용 가능' if db.has_fts(conn) else '미지원'}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pwc", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="데이터 디렉터리 (기본값: ./data)")
    sub = parser.add_subparsers(dest="command", required=True)

    only_kwargs = dict(nargs="*", choices=list(sources.DUMPS), default=None,
                       help="특정 덤프만 처리 (기본: 전체)")
    commands = [
        ("download", "아카이브 덤프 다운로드", cmd_download, True),
        ("ingest", "덤프를 SQLite로 적재", cmd_ingest, True),
        ("build", "download + ingest", cmd_build, True),
        ("collect", "신규 데이터 수집 (arXiv/HF/GitHub)", cmd_collect, False),
        ("stats", "적재 결과 요약", cmd_stats, False),
    ]
    for name, help_text, func, has_only in commands:
        p = sub.add_parser(name, help=help_text)
        # 서브커맨드 뒤에 와도 인식되도록 중복 정의한다. SUPPRESS 덕에
        # 서브커맨드 쪽에서 생략하면 최상위 값이 유지된다.
        p.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                       help="데이터 디렉터리 (기본값: ./data)")
        if has_only:
            p.add_argument("--only", **only_kwargs)
        if name == "collect":
            p.add_argument("--source", nargs="*",
                           choices=["arxiv", "hf", "hf-models", "github"],
                           default=["arxiv", "hf", "hf-models", "github"],
                           help="실행할 수집기 (기본: 전체)")
            p.add_argument("--max-results", type=int, default=500,
                           help="arXiv 최대 수집 편수")
            p.add_argument("--max-papers", type=int, default=25,
                           help="GitHub 링크 검색 대상 논문 수")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)
