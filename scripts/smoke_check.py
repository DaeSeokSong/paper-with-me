"""실데이터 기능 점검 스크립트.

실제 아카이브로 빌드된 SQLite에 대해 웹 앱의 모든 핵심 기능을 TestClient로
점검한다. 원본 paperswithcode.com에서 확실히 존재했던 콘텐츠(Attention Is All
You Need, ImageNet, Transformer 등)가 복원본에서도 조회되는지 확인한다.

사용법: PWC_DB=data/pwc.sqlite python scripts/smoke_check.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import create_app  # noqa: E402
from app import queries  # noqa: E402

# 페이지 응답시간 상한(초). CI 러너 변동성을 감안해 여유 있게 잡되,
# 과거처럼 분 단위로 걸리는 회귀는 확실히 잡는다.
SLOW = 20.0

failures: list[str] = []
_last_elapsed = 0.0


def timed_get(client: TestClient, path: str, **kwargs):
    global _last_elapsed
    t0 = time.monotonic()
    r = client.get(path, **kwargs)
    _last_elapsed = time.monotonic() - t0
    return r


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}"
          + (f" — {detail}" if detail else "")
          + (f" ({_last_elapsed:.1f}s)" if _last_elapsed else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    if not db_path.exists():
        print(f"DB가 없습니다: {db_path}", file=sys.stderr)
        return 2

    conn = queries.connect(db_path)
    st = queries.stats(conn)
    print("적재 통계:", ", ".join(f"{k}={v:,}" for k, v in st.items()))
    # 진단용: 실데이터 URL 형태 확인 (조회 최적화의 정확 일치 경로 검증)
    for row in conn.execute("SELECT paper_url FROM papers LIMIT 3"):
        print("  sample paper_url:", row["paper_url"])

    # 원본 서비스 규모 대비 하한선 (마지막 스냅샷 기준 대략치의 절반 이하로 잡음)
    check("papers >= 500k", st["papers"] >= 500_000, f"{st['papers']:,}")
    check("repos >= 100k", st["repos"] >= 100_000, f"{st['repos']:,}")
    check("sota_rows >= 100k", st["sota_rows"] >= 100_000, f"{st['sota_rows']:,}")
    check("tasks >= 1k", st["tasks"] >= 1_000, f"{st['tasks']:,}")
    check("datasets >= 3k", st["datasets"] >= 3_000, f"{st['datasets']:,}")
    check("methods >= 1k", st["methods"] >= 1_000, f"{st['methods']:,}")

    client = TestClient(create_app(db_path))
    slow_pages: list[tuple[str, float]] = []

    def get(path: str, **kwargs):
        r = timed_get(client, path, **kwargs)
        if _last_elapsed > SLOW:
            slow_pages.append((path, _last_elapsed))
        return r

    r = get("/")
    check("홈(트렌딩/최신/통계)", r.status_code == 200 and "Trending" in r.text)

    r = get("/paper/attention-is-all-you-need")
    check("논문 상세", r.status_code == 200 and "Attention" in r.text)
    check("논문-코드 링크", "github.com" in r.text)

    r = get("/search", params={"q": "diffusion model"})
    check("전문 검색", r.status_code == 200 and "/paper/" in r.text)

    r = get("/sota")
    check("SOTA task 목록", r.status_code == 200 and "Image Classification" in r.text)

    r = get("/sota/image-classification")
    check("task 벤치마크 목록 (Image Classification)",
          r.status_code == 200 and "ImageNet" in r.text)

    r = get("/sota/image-classification/imagenet")
    check("리더보드 표 (Image Classification on ImageNet)",
          r.status_code == 200 and "<table" in r.text)
    check("리더보드 지표 값 오염 없음 (None 노출 금지)", ">None<" not in r.text)

    r = get("/sota/image-classification/cifar-100")
    check("리더보드 표 (CIFAR-100) 지표 정상",
          r.status_code == 200 and ">None<" not in r.text
          and "Content Selection" not in r.text)

    r = get("/task/semantic-segmentation")
    check("원본 /task/ URL 호환", r.status_code == 200)

    r = get("/datasets", params={"q": "coco"})
    check("데이터셋 검색", r.status_code == 200 and "COCO" in r.text)

    r = get("/dataset/imagenet")
    check("데이터셋 상세 + 벤치마크 연결", r.status_code == 200)

    r = get("/methods", params={"q": "attention"})
    check("방법론 검색", r.status_code == 200)

    r = get("/method/transformer")
    check("방법론 상세 (Transformer)", r.status_code == 200)

    r = get("/trends")
    check("Trends (프레임워크 점유율)",
          r.status_code == 200 and "pytorch" in r.text.lower())

    r = get("/papers", params={"page": 2})
    check("논문 목록 페이지네이션", r.status_code == 200)

    r = get("/paper/definitely-not-a-real-paper-xyz")
    check("404가 HTML 에러 페이지로 렌더링",
          r.status_code == 404 and "<html" in r.text)

    # 리더보드 → 논문 페이지 연결 무결성 (papers 덤프에 없는 논문은 스텁으로)
    sample = conn.execute(
        """SELECT s.paper_url FROM sota_rows s
           WHERE s.paper_url LIKE 'https://paperswithcode.com/paper/%'
           ORDER BY s.id LIMIT 5"""
    ).fetchall()
    linked_ok = True
    for row in sample:
        slug = row["paper_url"].rstrip("/").rsplit("/", 1)[-1]
        if timed_get(client, f"/paper/{slug}").status_code != 200:
            linked_ok = False
            print(f"  깨진 논문 링크: /paper/{slug}")
    check("리더보드 논문 링크 연결 (스텁 폴백 포함)", linked_ok)

    missing = conn.execute(
        """SELECT COUNT(DISTINCT s.paper_url) FROM sota_rows s
           LEFT JOIN papers p ON p.paper_url = s.paper_url
           WHERE p.paper_url IS NULL AND s.paper_url IS NOT NULL"""
    ).fetchone()[0]
    print(f"  (진단) papers 덤프에 없는 리더보드 논문: {missing:,}편 — 스텁으로 서비스")

    # Phase 2 수집분이 검색에 반영되는지 (수집분이 있는 스냅샷에서만)
    fresh = conn.execute(
        "SELECT paper_url, title FROM papers WHERE source != 'archive' "
        "AND title IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if fresh:
        r = get("/search", params={"q": fresh["title"]})
        slug = fresh["paper_url"].rstrip("/").rsplit("/", 1)[-1]
        check("수집 논문 검색 노출 (FTS 동기화)",
              r.status_code == 200 and slug in r.text,
              fresh["title"][:60])
    else:
        print("[SKIP] 수집 논문 검색 — source != 'archive' 논문 없음")

    check(f"모든 페이지 응답 {SLOW:.0f}초 이내", not slow_pages,
          ", ".join(f"{p} {t:.1f}s" for p, t in slow_pages))

    print()
    if failures:
        print(f"{len(failures)}개 점검 실패: {failures}")
        return 1
    print("모든 기능 점검 통과 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
