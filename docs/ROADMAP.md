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
- [x] 전 영역 정밀 검수 — 6개 영역 서브 에이전트 검수(76건 발견) 후
      치명·높음·중간 등급 일괄 수정 (FTS 동기화, 데이터 소실 체인 이관,
      리더보드 원본 순서 보존, LIKE 이스케이프, 배포 원자성 등)
- [x] UI 리뉴얼 — 반응형; 기본 테마는 원본과 동일한 라이트,
      다크 모드는 헤더 토글로 선택 (원본에 없던 차별화 요소)
- [x] 논문-코드 링크 고도화 — HF model card(arxiv 필터) 기반 모델 매칭
      수집기 (`hf-models`)
- [x] arXiv 개정판(v2+) 추적 — lastUpdatedDate 수집 + 수집 논문 메타 갱신
- [x] 한글·부분어 검색 — 신규 빌드부터 FTS trigram 토크나이저
- [x] 리더보드 갱신 워크플로 — 커뮤니티 기여(PR) 기반 (contributions/)
- [x] 원본 동등성 1차 정합 — 1차 사료(원본 HTML 캡처) 기반: 흰 헤더,
      그라데이션 버튼, 차트 색, Rank/Year 컬럼, /sota 분야 그룹,
      ground truth 수치 게이트(CIFAR-100 96.08)
- [ ] is_official 추정 휴리스틱 (저자-저장소 소유자 매칭)
- [ ] UI 한국어/영어 전환
- [x] 사용성 베타 테스트 — 3개 관점(신규 사용자·모바일/접근성·파워유저)
      QA 검수 26건 반영: 탐색 왕복 복원, 죽은 길 제거, WCAG AA 대비·터치
      타겟, 통합 검색 매치, API 스텁 일관성, 공동 순위, 리더보드
      페이지네이션+금·은·동, 차트 축·제목 재설계
- [x] 리더보드 Extra Training Data 컬럼 + 데이터셋 표기 변형(variants)
      매칭 (재빌드 스냅샷부터 데이터 반영)
- [ ] 리더보드 2025-07 이후 갱신 자동화 — 신규 수집 논문에서 기존 벤치마크
      언급을 탐지해 기여 PR 초안을 생성하는 반자동 파이프라인 (현재는
      contributions/ PR 수동 기여만 가능; 원본 종료로 권위 소스 부재)
- [ ] 원본 잔여 격차 (동등성 검수 기록):
      리더보드 Tags/Result 컬럼,
      논문 카드 썸네일·"Ranked #N" 스파크라인 배지,
      /datasets 좌측 필터 패널(모달리티/task/언어),
      Methods 인덱스 카테고리 카드, task 페이지 Papers 목록·
      Most implemented 섹션, 원본 webfont 서체

## Phase 4 — 앱 (App)

- [x] 공개 JSON API v1 (`/api/v1/*`, OpenAPI 문서, CORS) — 앱 백엔드
- [x] PWA 기반 (manifest, 아이콘, theme-color) — 홈 화면 설치 지원
- [ ] 서비스워커 오프라인 셸 + 홈 화면 설치 유도
- [ ] 네이티브/크로스플랫폼 앱 (React Native/Flutter, API v1 사용)
- [ ] 앱 전용 기능 — 북마크, 새 논문 알림(푸시)

## Phase 3 — 배포 (Deploy)

- [ ] 호스팅 선정 (초기: 단일 인스턴스 + SQLite로 충분, 성장 시 Postgres 이관)
- [ ] 도메인/HTTPS, 검색엔진 색인
- [ ] 정적 스냅샷 다운로드 제공 (CC-BY-SA 유지)
- [ ] 모니터링/백업
