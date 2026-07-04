"""신규 수집 논문의 초록에서 리더보드 결과를 자동 추출한다 (Phase 2).

원본 paperswithcode.com도 논문 본문에서 수치를 추출(sota-extractor)하고
커뮤니티가 검수하는 방식으로 리더보드를 만들었다. 2025-07 원본 종료 후
권위 있는 SOTA 소스가 없으므로, 보수적인 휴리스틱으로 리더보드를 이어간다:

- "on {기존 벤치마크 dataset}" 문맥이 있는 초록만 대상
- 지표는 해당 보드에 이미 존재하는 지표 이름과 매칭될 때만
- 수치는 해당 지표의 기존 값 범위(±약간의 여유) 안일 때만
- 추가된 행은 source='auto'로 표시 — UI에서 "자동 추출" 배지로 구분

이 게이트를 전부 통과하지 못하면 조용히 버린다 (누락이 오염보다 낫다).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

# 짧은 데이터셋 이름("SST", "NLI" 등)은 초록에서 오탐이 잦다
MIN_DATASET_LEN = 4
# 데이터셋 언급 주변에서 지표·수치를 찾는 창(문자 수)
WINDOW = 260

_MODEL_RE = re.compile(
    r"(?:propose[sd]?|present[s]?|introduce[s]?|call(?:ed)?|named?|"
    r"dub(?:bed)?)\s+(?:a\s|an\s|the\s|novel\s)*"
    r"([A-Z][A-Za-z0-9+_.\-]{1,29})")
_MODEL_STOP = {"We", "Our", "In", "This", "The", "A", "An", "It", "To"}
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_NUMBER_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\b")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _benchmark_index(conn: sqlite3.Connection) -> dict:
    """dataset 소문자 → [{task, dataset, metrics: {이름: [기존 값들]}}].

    자동 추출 값의 sanity 검증에 기존(archive/contrib) 행만 쓴다 —
    auto 행을 포함하면 오염된 값이 다음 추출의 기준이 되어 표류한다.
    """
    boards: dict[tuple, dict] = {}
    for task, dataset, metrics_json in conn.execute(
        """SELECT task, dataset, metrics FROM sota_rows
           WHERE task IS NOT NULL AND dataset IS NOT NULL
             AND (source IS NULL OR source != 'auto')"""
    ):
        try:
            metrics = json.loads(metrics_json) if metrics_json else {}
        except ValueError:
            continue
        if not isinstance(metrics, dict):
            continue
        board = boards.setdefault((task, dataset), {})
        for name, value in metrics.items():
            try:
                v = float(str(value).replace("%", "").replace(",", "").strip())
            except ValueError:
                continue
            board.setdefault(name, []).append(v)
    index: dict[str, list[dict]] = {}
    for (task, dataset), metrics in boards.items():
        if len(dataset) < MIN_DATASET_LEN or not metrics:
            continue
        index.setdefault(dataset.lower(), []).append(
            {"task": task, "dataset": dataset, "metrics": metrics})
    return index


def _model_name(title: str, abstract: str) -> str:
    m = _MODEL_RE.search(abstract or "")
    if m and m.group(1) not in _MODEL_STOP:
        return m.group(1)
    # "GreatNet: a method for ..." 형태의 제목 접두
    head = (title or "").split(":", 1)[0].strip()
    if 0 < len(head) <= 30 and len(head.split()) <= 3:
        return head
    return (title or "")[:40]


def _match_metric(board_metrics: dict, window_norm: str) -> str | None:
    """창 안에서 언급된 보드 지표를 찾는다. 정확 명칭 우선, 정확도 계열은
    'accuracy' 언급으로 폴백."""
    for name in board_metrics:
        if _norm(name) and _norm(name) in window_norm:
            return name
    if "accuracy" in window_norm:
        for name in board_metrics:
            if re.search(r"accuracy|percentage correct|top 1", name, re.I):
                return name
    return None


def _sane(value: float, existing: list[float]) -> bool:
    """기존 보드 값 분포 기준의 타당성 검사.

    신규 SOTA는 본질적으로 기존 최고치를 넘으므로 여유를 분포 폭의
    절반까지 준다. 퍼센트형 지표(기존 값이 0~100 안)는 0~100으로 상한 —
    '196.08' 같은 오추출을 차단한다.
    """
    if not existing:
        return False
    lo, hi = min(existing), max(existing)
    span = (hi - lo) or max(abs(hi), 1.0)
    if not ((lo - 0.5 * span) <= value <= (hi + 0.5 * span)):
        return False
    if 0 <= lo and hi <= 100 and not (0 <= value <= 100):
        return False
    return True


def extract_from_text(title: str, abstract: str, index: dict) -> list[dict]:
    """초록에서 (task, dataset, metric, value) 후보를 뽑는다."""
    text = f"{title or ''}. {abstract or ''}"
    lower = text.lower()
    out = []
    seen: set[tuple] = set()
    for ds_lower, boards in index.items():
        if ds_lower not in lower:
            continue  # 빠른 사전 필터
        for m in re.finditer(
                rf"\bon (?:the )?{re.escape(ds_lower)}\b", lower):
            start = max(0, m.start() - WINDOW)
            window = text[start:m.end() + WINDOW]
            window_norm = _norm(window)
            # 수치: % 표기 우선, 없으면 일반 숫자 중 데이터셋 언급에 근접한 것
            nums = [(abs((start + x.start()) - m.start()), x.group(1))
                    for x in _PERCENT_RE.finditer(window)]
            if not nums:
                nums = [(abs((start + x.start()) - m.start()), x.group(1))
                        for x in _NUMBER_RE.finditer(window)]
            nums.sort()
            for board in boards:
                metric = _match_metric(board["metrics"], window_norm)
                if not metric:
                    continue
                key = (board["task"], board["dataset"], metric)
                if key in seen:
                    continue
                for _, raw in nums:
                    value = float(raw)
                    if _sane(value, board["metrics"][metric]):
                        seen.add(key)
                        out.append({"task": board["task"],
                                    "dataset": board["dataset"],
                                    "metric": metric, "value": raw})
                        break
    return out


def collect(conn: sqlite3.Connection, max_papers: int = 300) -> int:
    """추출을 아직 시도하지 않은 신규 수집 논문을 처리한다 (멱등)."""
    papers = conn.execute(
        """SELECT paper_url, title, abstract, date FROM papers
           WHERE source != 'archive' AND abstract IS NOT NULL
             AND paper_url NOT IN (SELECT paper_url FROM result_extract_log)
           ORDER BY date DESC LIMIT ?""",
        (max_papers,),
    ).fetchall()
    if not papers:
        print("[results] 추출 대상 신규 논문 없음", flush=True)
        return 0
    index = _benchmark_index(conn)
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for paper_url, title, abstract, date in papers:
        for cand in extract_from_text(title, abstract, index):
            exists = conn.execute(
                "SELECT 1 FROM sota_rows WHERE task=? AND dataset=? "
                "AND paper_url=?",
                (cand["task"], cand["dataset"], paper_url),
            ).fetchone()
            if exists:
                continue
            code_links = [
                {"title": r[0].rstrip("/").rsplit("/", 1)[-1], "url": r[0]}
                for r in conn.execute(
                    "SELECT repo_url FROM repos WHERE paper_url = ? LIMIT 3",
                    (paper_url,))
            ]
            conn.execute(
                """INSERT INTO sota_rows
                   (task, parent_task, dataset, model_name, metrics,
                    paper_url, paper_title, paper_date, code_links, source)
                   VALUES (?,?,?,?,?,?,?,?,?, 'auto')""",
                (cand["task"], None, cand["dataset"],
                 _model_name(title, abstract),
                 json.dumps({cand["metric"]: cand["value"]},
                            ensure_ascii=False),
                 paper_url, title, date,
                 json.dumps(code_links, ensure_ascii=False)),
            )
            added += 1
            print(f"[results] + {cand['task']} / {cand['dataset']} "
                  f"{cand['metric']}={cand['value']} ← {title[:60]}",
                  flush=True)
        conn.execute(
            "INSERT OR REPLACE INTO result_extract_log VALUES (?, ?)",
            (paper_url, now),
        )
    conn.commit()
    print(f"[results] {len(papers)}편 검사, {added}행 추가", flush=True)
    return added
