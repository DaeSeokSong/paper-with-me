# 기여 가이드

## 리더보드 결과 제출 (커뮤니티 기여)

원본 Papers with Code의 "결과 제출"을 GitHub PR 방식으로 대체합니다.

1. `contributions/` 디렉터리에 JSON 파일을 추가하세요 (파일명 자유,
   `.json` 확장자). 하나의 파일에 객체 하나 또는 배열로 여러 건 가능:

```json
{
  "task": "Image Classification",
  "dataset": "CIFAR-100",
  "model_name": "MyNet-XL",
  "metrics": { "Percentage correct": "96.90" },
  "paper_url": "https://arxiv.org/abs/2507.12345",
  "paper_title": "MyNet: A Better Network",
  "paper_date": "2026-07-01",
  "code_links": [
    { "title": "me/mynet", "url": "https://github.com/me/mynet" }
  ]
}
```

2. 필수 필드: `task`, `dataset`, `model_name`, `metrics`(비어있지 않은 객체).
   `paper_date`는 `YYYY-MM-DD`, URL류는 http(s)여야 합니다.
3. PR을 올리면 CI가 스키마를 자동 검증합니다.
4. 머지되면 다음 일일 갱신(03:00 UTC)에서 리더보드에 반영됩니다.
   기존 task/dataset 이름과 정확히 일치해야 같은 리더보드에 표시됩니다.

## 코드 기여

- 테스트: `pip install -e ".[dev,stream,web]" && pytest`
- 코드는 AGPL-3.0으로 배포됩니다. 데이터 기여는 CC-BY-SA 4.0을 따릅니다.
