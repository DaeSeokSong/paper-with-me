# 아키텍처

## 원칙

- **원본 충실 복원 우선**: 원본 서비스(Django 기반)는 비공개였으므로 코드가 아닌 *데이터와 기능*을 복원한다. 데이터 포맷은 원본 공개 덤프(sota-extractor JSON 포맷)를 그대로 따른다.
- **단순하게 시작**: 아카이브는 불변 데이터이므로 초기에는 SQLite 하나로 충분하다. 갱신 파이프라인(Phase 2)이 붙을 때 필요해지면 Postgres로 이관한다.
- **환경 독립성**: 파이프라인은 표준 라이브러리만으로 동작(선택적으로 ijson). 네트워크가 제한된 환경을 위해 GitHub Actions로도 스냅샷을 빌드할 수 있다.

## 데이터 소스

Hugging Face `pwc-archive` 조직의 5개 데이터셋 저장소. 파일명은 하드코딩하지 않고 HF API(`/api/datasets/{id}/tree/main`)로 런타임에 탐색하여 `.json.gz`/`.json` 파일을 선택한다 (아카이브 저장소의 파일 구성 변화에 견고).

## DB 스키마 (SQLite)

```
papers        논문 (paper_url PK, arxiv_id, title, abstract, url_abs, url_pdf,
              proceeding, date, authors/tasks/methods는 JSON 컬럼)
repos         논문-코드 링크 (paper_url, repo_url, is_official, framework, ...)
datasets      데이터셋 카탈로그 (url PK, name, full_name, homepage, ...)
methods       방법론 카탈로그 (url PK, name, full_name, intro_year, ...)
sota_rows     리더보드 행 (task, dataset, model_name, metrics JSON, paper_url, ...)
              — evaluation-tables의 중첩 구조(task → subtasks → datasets → sota.rows)를
                재귀적으로 평탄화하여 적재
papers_fts    FTS5 전문 검색 (title + abstract), 미지원 빌드에서는 자동 생략
```

정규화보다 원본 덤프 구조 보존을 우선한다(리스트형 필드는 JSON 텍스트로 저장). 웹 앱이 필요로 하는 조회 패턴이 확정되면 인덱스/뷰를 추가한다.

## 웹 앱 (Phase 1 후반)

- Python (FastAPI + Jinja2 서버 렌더링) — 파이프라인과 언어 통일, 배포 단순
- 읽기 전용이므로 SQLite를 직접 조회, 페이지 캐싱으로 충분
- URL 구조는 원본을 따른다: `/paper/{slug}`, `/task/{slug}`, `/sota/{slug}`, `/dataset/{slug}`, `/method/{slug}`

## 갱신 파이프라인 (Phase 2)

아카이브 테이블과 동일 스키마에 `source` 컬럼(archive | arxiv | hf | community)을 추가하여 증분 수집분을 구분 적재한다. 수집기는 소스별 독립 모듈로 두고 GitHub Actions 스케줄로 구동한다.
