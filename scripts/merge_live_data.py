"""아카이브 재빌드에 기존 스냅샷의 수집 누적분을 이관한다.

build-data는 항상 빈 DB에서 아카이브만 다시 만들기 때문에, 그대로 두면
update-data가 매일 쌓아온 신규 논문(source != 'archive')·코드 링크·인기
신호(signals)가 아티팩트 교체 시점에 통째로 사라진다. 재빌드 직후 이
스크립트로 이전 스냅샷의 수집분을 새 DB에 합친다.

사용법: python scripts/merge_live_data.py <새 DB> <이전 스냅샷 DB>
이전 스냅샷이 없으면(첫 빌드) 정상 종료한다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pwc import db  # noqa: E402


def merge(new_db: Path, old_db: Path) -> dict[str, int]:
    conn = db.connect(new_db)  # 스키마/트리거 보장
    conn.execute("ATTACH DATABASE ? AS old", (str(old_db),))
    counts: dict[str, int] = {}

    # 신규 논문 — 아카이브에 이미 있는 것(같은 paper_url/arxiv_id)은 제외
    counts["papers"] = conn.execute(
        """INSERT OR IGNORE INTO papers
           (paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
            proceeding, date, authors, tasks, methods, source)
           SELECT paper_url, arxiv_id, title, abstract, url_abs, url_pdf,
                  proceeding, date, authors, tasks, methods, source
           FROM old.papers o
           WHERE o.source != 'archive'
             AND (o.arxiv_id IS NULL OR o.arxiv_id NOT IN
                  (SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL))"""
    ).rowcount

    counts["repos"] = conn.execute(
        """INSERT OR IGNORE INTO repos
           (paper_url, repo_url, is_official, framework,
            mentioned_in_paper, mentioned_in_github, source, stars)
           SELECT paper_url, repo_url, is_official, framework,
                  mentioned_in_paper, mentioned_in_github, source, stars
           FROM old.repos WHERE source != 'archive'"""
    ).rowcount

    counts["signals"] = conn.execute(
        """INSERT OR IGNORE INTO signals
           (paper_url, github_stars, hf_upvotes, updated_at)
           SELECT paper_url, github_stars, hf_upvotes, updated_at
           FROM old.signals"""
    ).rowcount

    # 커뮤니티 기여·자동 추출 리더보드 행 — 재빌드가 이걸 버리면 기여는
    # 다음 collect까지 공백, auto는 영구 유실이었다 (코드 리뷰 발견)
    # tags 컬럼이 없는 구 스냅샷도 이관 가능해야 한다
    old_cols = {r[1] for r in conn.execute("PRAGMA old.table_info(sota_rows)")}
    tags_sel = "o.tags" if "tags" in old_cols else "NULL"
    counts["sota_rows"] = conn.execute(
        f"""INSERT INTO sota_rows
           (task, parent_task, dataset, model_name, metrics, paper_url,
            paper_title, paper_date, code_links, metrics_order, area,
            uses_additional_data, source, tags)
           SELECT o.task, o.parent_task, o.dataset, o.model_name, o.metrics,
                  o.paper_url, o.paper_title, o.paper_date, o.code_links,
                  o.metrics_order, o.area, o.uses_additional_data, o.source,
                  {tags_sel}
           FROM old.sota_rows o
           WHERE o.source IN ('contrib', 'auto', 'external')
             AND NOT EXISTS (SELECT 1 FROM sota_rows n
                             WHERE n.task = o.task AND n.dataset = o.dataset
                               AND n.paper_url = o.paper_url)"""
    ).rowcount

    # 검색 이력 — 없으면 재빌드마다 GitHub/HF 쿼터가 0건 확인된 논문
    # 재검색에 소모된다
    for log in ("repo_search_log", "model_search_log"):
        counts[log] = conn.execute(
            f"""INSERT OR IGNORE INTO {log} (paper_url, searched_at)
                SELECT paper_url, searched_at FROM old.{log}"""
        ).rowcount

    # 이관된 논문의 tasks가 태그 역인덱스에 반영되도록 플래그 무효화
    conn.execute("DELETE FROM meta WHERE key = 'papers_tasks_built'")

    conn.commit()
    conn.execute("DETACH DATABASE old")
    # 이관된 논문의 검색 인덱스 반영 (트리거가 INSERT를 처리하지만,
    # 구 스냅샷에 트리거 이전 유입분이 있었던 경우까지 멱등 보정)
    db.sync_fts(conn)
    conn.close()
    return counts


def main() -> int:
    new_db, old_db = Path(sys.argv[1]), Path(sys.argv[2])
    if not old_db.exists():
        print("[merge] 이전 스냅샷 없음 — 이관 건너뜀 (첫 빌드)")
        return 0
    if not new_db.exists():
        print(f"[merge] 새 DB가 없습니다: {new_db}", file=sys.stderr)
        return 1
    counts = merge(new_db, old_db)
    print("[merge] 수집 누적분 이관 완료:",
          ", ".join(f"{k} +{v:,}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
