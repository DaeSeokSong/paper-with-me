"""Phase 2 수집기 — 2025-07 아카이브 스냅샷 이후의 공백을 채운다.

- arxiv: arXiv API에서 최신 ML 논문 수집
- hf: Hugging Face Daily Papers (논문 + 업보트 신호)
- github: 논문 ↔ 코드 저장소 매칭 + 스타 수 신호

모든 수집기는 아카이브와 동일한 테이블에 source 컬럼으로 구분 적재하며,
이미 있는 논문(arxiv_id 기준)은 건드리지 않는다 (아카이브가 우선).
"""
