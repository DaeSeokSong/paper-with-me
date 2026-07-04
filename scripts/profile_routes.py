"""느린 라우트의 병목을 실데이터에서 컴포넌트별로 계측한다.

TestClient 경유 cProfile은 앱이 별도 스레드에서 돌아 내부가 보이지 않으므로,
쿼리·렌더링을 직접 호출해 단계별 시간을 잰다.

사용법: PWC_DB=data/pwc.sqlite python scripts/profile_routes.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from app import queries  # noqa: E402
from app.main import TEMPLATES_DIR  # noqa: E402


def timed(label: str, fn, *args, **kwargs):
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    print(f"  {label}: {time.monotonic() - t0:.2f}s", flush=True)
    return result


def main() -> int:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    conn = timed("connect", queries.connect, db_path)

    print("== 논문 상세 ==", flush=True)
    exact = timed("get_paper 정확 일치 쿼리", lambda: conn.execute(
        "SELECT paper_url FROM papers WHERE paper_url = ?",
        ("https://paperswithcode.com/paper/attention-is-all-you-need",),
    ).fetchone())
    print(f"  정확 일치 결과: {exact and exact['paper_url']}", flush=True)
    timed("get_paper 전체(폴백 포함)", queries.get_paper, conn,
          "attention-is-all-you-need")

    print("== 리더보드 (Image Classification on ImageNet) ==", flush=True)
    task = timed("find_task", queries.find_task, conn, "image-classification")
    dataset = timed("find_benchmark_dataset", queries.find_benchmark_dataset,
                    conn, task, "imagenet")
    print(f"  task={task!r}, dataset={dataset!r}", flush=True)

    rows = timed("행 조회(fetchall)", lambda: conn.execute(
        "SELECT * FROM sota_rows WHERE task = ? AND dataset = ?",
        (task, dataset),
    ).fetchall())
    print(f"  행 수: {len(rows)}", flush=True)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM sota_rows WHERE task=? AND dataset=?",
        (task, dataset),
    ).fetchall()
    print("  쿼리 플랜:", [tuple(p) for p in plan], flush=True)

    board = timed("dataset_leaderboard 전체", queries.dataset_leaderboard,
                  conn, task, dataset)
    print(f"  지표 컬럼: {board['metric_names']}", flush=True)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    env.filters["paper_slug"] = queries.paper_slug
    env.filters["slugify"] = queries.slugify
    template = env.get_template("board.html")
    # 라우트와 동일한 페이지네이션 컨텍스트로 렌더링
    per = queries.BOARD_PAGE_SIZE
    html = timed("템플릿 렌더링", template.render, task=task,
                 task_slug="image-classification", board=board, q="",
                 rows=board["rows"][:per], page=1, per=per, offset=0,
                 total=len(board["rows"]))
    print(f"  HTML 크기: {len(html) / 1e6:.1f}MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
