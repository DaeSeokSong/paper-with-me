# 기능 복원 점검표

원본 paperswithcode.com(2025-07 마지막 스냅샷 기준)의 기능 대비 복원 상태.
"실데이터 점검"은 `scripts/smoke_check.py`가 GitHub Actions(`Build data snapshot`)에서
실제 아카이브 데이터로 자동 검증하는 항목이다.

| 원본 기능 | 복원 상태 | 경로 | 실데이터 점검 |
|---|---|---|---|
| 홈 — Trending papers | ✅ 근사 복원 (구현 수 × 최신성; 아카이브에 실시간 스타 수 없음) | `/` | ✅ |
| 논문 목록/페이지네이션 | ✅ | `/papers` | ✅ |
| 논문 상세 (초록·저자·arXiv 링크) | ✅ | `/paper/{slug}` | ✅ |
| 논문 ↔ 코드 구현 (공식 구현 표시, 프레임워크) | ✅ | `/paper/{slug}` | ✅ |
| 논문 검색 (제목·초록 전문 검색) | ✅ FTS5 | `/search` | ✅ |
| Browse State-of-the-Art (task 목록) | ✅ | `/sota` | ✅ |
| task별 벤치마크 목록 | ✅ | `/sota/{task}`, `/task/{task}` | ✅ |
| 리더보드 순위표 (dataset별 페이지, 원본 구조) | ✅ 최빈 지표 8개 컬럼, 첫 지표 기준 정렬 | `/sota/{task}/{dataset}` | ✅ (20초 응답 상한 게이트 포함) |
| 논문별 Results (벤치마크 성적) | ✅ | `/paper/{slug}` | — |
| Datasets 카탈로그 + 검색 | ✅ | `/datasets`, `/dataset/{slug}` | ✅ |
| Methods 카탈로그 + 검색 | ✅ | `/methods`, `/method/{slug}` | ✅ |
| Trends (프레임워크 점유율 추이) | ✅ | `/trends` | ✅ |
| GitHub 스타 수 / 실시간 트렌딩 | ⏳ Phase 2 (아카이브에 없음 → GitHub API로 재수집) | — | — |
| 스냅샷 이후(2025-07~) 신규 논문 | ⏳ Phase 2 (arXiv/HF Papers 수집기) | — | — |
| 사용자 계정·논문/결과 제출·편집 | ⏳ Phase 2 (커뮤니티 기여는 GitHub PR 기반으로 대체 예정) | — | — |
| 포털 (천문학·물리 등 분야별 사이트) | ❌ 범위 외 | — | — |
| 뉴스레터 | ❌ 범위 외 | — | — |
