# paper-with-me

> Papers with Code 복원 프로젝트 — 사라진 paperswithcode.com을 되살리고, 현재 시대에 맞게 발전시켜 다시 배포합니다.

## 배경

[Papers with Code](https://paperswithcode.com)는 ML 논문·코드 구현·SOTA 리더보드·데이터셋·방법론(Methods)을 한곳에서 탐색할 수 있던 서비스였으나, 2025년 7월 Meta가 서비스를 종료하면서 도메인이 Hugging Face의 Trending Papers로 리다이렉트되고 기존 기능(리더보드 9,300여 개, 논문-코드 링크 약 8만 건, 데이터셋 5,600여 개)은 사라졌습니다.

다행히 마지막 공개 스냅샷이 커뮤니티에 의해 보존되어 있습니다:

- Hugging Face [`pwc-archive`](https://huggingface.co/pwc-archive) 조직 — 원본 데이터 덤프 아카이브
- GitHub [`paperswithcode/paperswithcode-data`](https://github.com/paperswithcode/paperswithcode-data) — 덤프 포맷 문서 (sota-extractor JSON 포맷)

데이터 라이선스는 **CC-BY-SA 4.0**으로, 출처 표기와 동일 조건 공유 하에 복원·재배포가 가능합니다.

## 목표

1. **복원 (Phase 1)** — 아카이브 데이터를 적재하고 기존 Papers with Code의 핵심 기능(논문 탐색/검색, 논문-코드 연결, SOTA 리더보드, 데이터셋/방법론 카탈로그)을 그대로 되살립니다.
2. **현대화 (Phase 2)** — 스냅샷 이후(2025-07~)의 공백을 arXiv API, Hugging Face Papers, GitHub API 등 현행 소스로 채우고 지속 갱신 파이프라인을 구축합니다.
3. **배포 (Phase 3)** — 누구나 쓸 수 있게 공개 서비스로 배포합니다.

자세한 계획은 [docs/ROADMAP.md](docs/ROADMAP.md), 설계는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)를 참고하세요.

## 데이터 파이프라인 (Phase 1 — 1단계)

아카이브 덤프 5종을 내려받아 하나의 SQLite DB(`data/pwc.sqlite`)로 적재합니다.

| 덤프 | 내용 |
|---|---|
| `papers-with-abstracts` | 전체 논문 + 초록 |
| `links-between-paper-and-code` | 논문 ↔ 코드 저장소 링크 |
| `evaluation-tables` | 벤치마크/SOTA 리더보드 |
| `methods` | 방법론 카탈로그 |
| `datasets` | 데이터셋 카탈로그 |

### 사용법

```bash
pip install -e ".[stream]"        # ijson 포함(대용량 스트리밍 파싱), 순수 표준 라이브러리로도 동작

python -m pwc download            # HF pwc-archive에서 덤프 다운로드 → data/raw/
python -m pwc ingest              # 파싱 후 SQLite 적재 → data/pwc.sqlite
python -m pwc build               # download + ingest 한 번에
python -m pwc stats               # 적재 결과 요약
```

> 참고: Hugging Face 접근이 차단된 환경에서는 GitHub Actions 워크플로(`.github/workflows/build-data.yml`)가 데이터 스냅샷 아티팩트를 빌드합니다 (워크플로 파일 변경 push 시 자동 실행, 수동 실행도 가능). 이 워크플로는 실데이터로 웹 앱 기능 점검(`scripts/smoke_check.py`)까지 수행합니다.

## 웹 앱 (Phase 1 — 복원)

원본 paperswithcode.com의 URL 구조와 핵심 기능을 재현한 읽기 전용 웹 앱입니다.
기능별 복원 상태는 [docs/FEATURES.md](docs/FEATURES.md) 참고.

```bash
pip install -e ".[web]"
PWC_DB=data/pwc.sqlite uvicorn app.main:app   # http://127.0.0.1:8000
```

| 경로 | 기능 |
|---|---|
| `/` | 홈 — Trending / Latest / 전체 통계 |
| `/papers`, `/paper/{slug}` | 논문 목록·상세 (초록, 코드 구현, 벤치마크 결과) |
| `/search?q=` | 제목·초록 전문 검색 (FTS5) |
| `/sota`, `/sota/{task}` (= `/task/{task}`) | Browse State-of-the-Art, dataset별 리더보드 |
| `/datasets`, `/dataset/{slug}` | 데이터셋 카탈로그 |
| `/methods`, `/method/{slug}` | 방법론 카탈로그 |
| `/trends` | 프레임워크 점유율 추이 |

### 테스트

```bash
pip install -e ".[dev]"
pytest
```

## 프로젝트 구조

```
pwc/            데이터 파이프라인 패키지
  sources.py    아카이브 덤프 소스 정의 + HF 파일 탐색
  download.py   덤프 다운로드
  db.py         SQLite 스키마
  ingest.py     덤프 → DB 적재
  cli.py        CLI (download/ingest/build/stats)
tests/          오프라인 실행 가능한 테스트 (fixtures 포함)
docs/           로드맵, 아키텍처 문서
```

## 라이선스

- 데이터: [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) (원 출처: Papers with Code / `pwc-archive`)
- 코드: 추후 결정
