"""CLI: python -m pwc {download,ingest,build,stats}"""

from __future__ import annotations

import argparse
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
    if cmd_download(args) != 0:
        return 1
    return cmd_ingest(args)


def cmd_stats(args: argparse.Namespace) -> int:
    db_path = args.data_dir / "pwc.sqlite"
    if not db_path.exists():
        print(f"DB가 없습니다: {db_path}", file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    for table in ("papers", "repos", "datasets", "methods", "sota_rows"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:>10}: {n:,}")
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
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)
