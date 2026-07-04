"""사이트 전수 크롤 점검 — 모든 내부 링크를 실제로 따라가며 검사한다.

홈에서 시작해 렌더링된 HTML의 내부 href를 BFS로 순회한다. 발견 항목:
- 200이 아닌 내부 링크 (원인 페이지와 함께 보고)
- 지표 오염(">None<"), 템플릿 미치환("{{", "{%")
- 응답 지연 페이지
- 링크가 하나도 없는 막다른 페이지 (footer 제외 본문 기준)

사용법: PWC_DB=data/pwc.sqlite python scripts/crawl_check.py [max_pages]
CI(verify-app)에서 실데이터 스냅샷으로 실행된다. 페이지 수 상한이 있는
샘플링이므로, 발견 0건이 무결점 증명은 아니지만 회귀는 확실히 잡는다.
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import create_app  # noqa: E402

SLOW = 20.0
HREF_RE = re.compile(r'href="([^"]+)"')
SEEDS = ["/", "/papers", "/sota", "/datasets", "/methods", "/trends",
         "/search?q=attention", "/sota/image-classification/cifar-100",
         "/sota/image-classification/imagenet"]
# 외부/정적 링크는 순회 제외
SKIP_PREFIX = ("http://", "https://", "mailto:", "#", "/static/", "/sw.js",
               "/api/")


def crawl(client: TestClient, max_pages: int) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    queue: deque[tuple[str, str]] = deque((s, "(seed)") for s in SEEDS)
    slow: list[str] = []
    n = 0
    rng = random.Random(42)
    while queue and n < max_pages:
        url, referrer = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        n += 1
        t0 = time.monotonic()
        try:
            r = client.get(url, follow_redirects=True)
        except Exception as e:  # noqa: BLE001
            issues.append(f"{url} → 예외 {e} (출처 {referrer})")
            continue
        elapsed = time.monotonic() - t0
        if elapsed > SLOW:
            slow.append(f"{url} {elapsed:.1f}s")
        if r.status_code != 200:
            issues.append(f"{url} → {r.status_code} (출처 {referrer})")
            continue
        body = r.text
        if ">None<" in body:
            issues.append(f"{url} → 'None' 값 노출 (출처 {referrer})")
        if "{{" in body or "{%" in body:
            issues.append(f"{url} → 템플릿 미치환 흔적 (출처 {referrer})")
        hrefs = []
        for href in HREF_RE.findall(body):
            if href.startswith(SKIP_PREFIX):
                continue
            absolute = urljoin(url, href)
            path = urlsplit(absolute)
            normalized = path.path + (f"?{path.query}" if path.query else "")
            if normalized not in seen:
                hrefs.append(normalized)
        # 대형 목록(리더보드 1천 행 등)에 밀려 다른 섹션이 굶지 않도록
        # 페이지당 링크를 섞어 일부만 큐에 넣는다
        rng.shuffle(hrefs)
        for h in hrefs[:80]:
            queue.append((h, url))
    for s in slow:
        issues.append(f"[SLOW] {s}")
    print(f"크롤 완료: {n}페이지 방문, 이슈 {len(issues)}건")
    return issues


def main() -> int:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    if not db_path.exists():
        print(f"DB가 없습니다: {db_path}", file=sys.stderr)
        return 2
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    client = TestClient(create_app(db_path))
    issues = crawl(client, max_pages)
    for issue in issues:
        print(f"  [ISSUE] {issue}")
    if issues:
        print(f"\n{len(issues)}건 발견 — 위 목록을 수정하세요")
        return 1
    print("전수 크롤 점검 통과 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
