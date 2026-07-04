# 로드맵

## Phase 1 — 복원 (Restore)

목표: 마지막 아카이브 스냅샷(2025-07) 기준으로 Papers with Code의 핵심 기능을 그대로 되살린다.

- [x] 아카이브 데이터 소스 확정 — HF `pwc-archive` 5종 덤프, CC-BY-SA 4.0
- [x] 데이터 파이프라인 — 덤프 다운로드 → SQLite 적재 (`pwc` 패키지)
- [x] 데이터 스냅샷 빌드 — GitHub Actions로 `pwc.sqlite` 생성 + 실데이터 기능 점검
- [x] 웹 앱 (읽기 전용 복원)
  - [x] 논문 목록/상세 + 전문 검색 (제목·초록 FTS)
  - [x] 논문-코드 저장소 링크 표시
  - [x] SOTA 리더보드 (task → dataset → 순위표, 원본 UI 구조 재현)
  - [x] 데이터셋 카탈로그
  - [x] 방법론(Methods) 카탈로그
  - [x] Browse by Task (`/sota` task 목록; 계층 트리 뷰는 개선 예정)
  - [x] Trends (프레임워크 점유율)
- [x] 실데이터 기준 성능 확보 — 전 페이지 응답 20초 상한 게이트 통과
      (리더보드 66s→1.3s, task 페이지 317s→0.02s, 홈 38s→0.1s, 논문 상세 3s→0.01s)

## Phase 2 — 현대화 (Modernize)

목표: 2025-07 스냅샷 이후의 공백을 채우고, 죽은 아카이브가 아닌 살아있는 서비스로 만든다.

- [x] 신규 논문 수집 — arXiv API (cs.LG/CV/CL/AI/NE/RO, stat.ML), source 컬럼으로 구분
- [x] 논문-코드 링크 1차 — GitHub 저장소 검색(arXiv ID 언급) 기반 매칭
- [x] Hugging Face Daily Papers 연동 — 신규 논문 + 업보트 신호
- [x] 인기 신호 반영 트렌딩 — signals(업보트·스타) 기반 홈 Trending 정렬
- [x] 일일 갱신 워크플로 — update-data (스냅샷 아티팩트 증분 갱신, 03:00 UTC)
- [ ] 논문-코드 링크 고도화 — README 인용·HF model card 링크 매칭
- [ ] 리더보드 갱신 워크플로 — 커뮤니티 기여(PR) 기반 + 반자동 추출
- [ ] UI 리뉴얼 — 반응형, 다크 모드, 한국어/영어

## Phase 3 — 배포 (Deploy)

- [ ] 호스팅 선정 (초기: 단일 인스턴스 + SQLite로 충분, 성장 시 Postgres 이관)
- [ ] 도메인/HTTPS, 검색엔진 색인
- [ ] 정적 스냅샷 다운로드 제공 (CC-BY-SA 유지)
- [ ] 모니터링/백업
