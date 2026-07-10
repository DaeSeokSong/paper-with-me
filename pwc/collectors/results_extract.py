"""신규 수집 논문의 초록에서 리더보드 결과를 자동 추출한다 (Phase 2).

원본 paperswithcode.com도 논문 본문에서 수치를 추출(sota-extractor)하고
커뮤니티가 검수하는 방식으로 리더보드를 만들었다. 2025-07 원본 종료 후
권위 있는 SOTA 소스가 없으므로, 보수적인 휴리스틱으로 리더보드를 이어간다:

- "on {기존 벤치마크 dataset}" 또는 "{dataset} benchmark/dataset" 문맥이
  있는 초록만 대상 (+ task 이름이 본문에 실제 언급된 보드만)
- 지표는 해당 보드에 이미 존재하는 지표 이름과 매칭될 때만 — 정확 명칭
  우선, 지표명 토큰 시그널(bleu/miou/f1/psnr 등) 폴백
- 수치는 % 표기이거나, %-없는 소수는 지표 언급과 80자 이내 인접일 때만
  ("1.31x speedup" 같은 배속·계수 오인 방지)
- 수치는 해당 지표의 기존 값 범위(±분포 폭의 절반) 안일 때만
- 추가된 행은 source='auto'로 표시 — UI에서 "자동 추출" 배지로 구분

이 게이트를 전부 통과하지 못하면 조용히 버린다 (누락이 오염보다 낫다).
"""

from __future__ import annotations

import json
import re
import sqlite3

# 짧은 데이터셋 이름("SST", "NLI" 등)은 초록에서 오탐이 잦다
MIN_DATASET_LEN = 4
# 데이터셋 언급 주변에서 지표·수치를 찾는 창(문자 수)
WINDOW = 260

_MODEL_RE = re.compile(
    r"(?:propose[sd]?|present[s]?|introduce[s]?|call(?:ed)?|named?|"
    r"dub(?:bed)?)\s+(?:a\s|an\s|the\s|novel\s)*"
    r"([A-Z][A-Za-z0-9+_.\-]{1,29})")
_MODEL_STOP = {"We", "Our", "In", "This", "The", "A", "An", "It", "To"}
# % 수치, 또는 단위 접미(x/×/M/B 등) 없는 순수 소수 — 정수는 연도·개수
# 오인이 잦아 소수점 필수
_NUM_RE = re.compile(
    r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%"
    r"|(?<![A-Za-z\d.%])(?P<plain>\d{1,3}\.\d+)(?![A-Za-z\d%×])")
# %-없는 수치가 유효하려면 지표 언급과 이 거리(문자) 안에 있어야 한다
METRIC_ADJACENCY = 80
# 지표명 토큰 시그널에서 제외할 일반어 (단독으로는 지표를 특정 못 함)
_TOKEN_STOP = {"score", "mean", "average", "overall", "test", "val",
               "set", "number", "metric", "the", "of"}


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


def _metric_signals(name: str) -> list[str]:
    """지표명에서 지표를 특정할 수 있는 토큰들 (bleu, miou, f1, psnr …)."""
    toks = [t for t in _norm(name).split()
            if len(t) >= 2 and not t.isdigit() and t not in _TOKEN_STOP]
    return toks


def _find_all(pattern: str, window_lower: str) -> list[int]:
    return [m.start() for m in re.finditer(pattern, window_lower)]


def _match_metric(board_metrics: dict,
                  window_lower: str) -> tuple[str | None, list[int]]:
    """창 안에서 언급된 보드 지표와 그 언급 위치들을 찾는다.

    정확 명칭(구분자 유연) 우선 → 지표명 토큰 시그널 → 정확도 계열의
    'accuracy' 언급 폴백. 위치는 %-없는 수치의 인접성 검사에 쓴다.
    """
    for name in board_metrics:
        toks = _norm(name).split()
        if not toks:
            continue
        pat = r"\b" + r"[^a-z0-9]{1,3}".join(map(re.escape, toks)) + r"\b"
        pos = _find_all(pat, window_lower)
        if pos:
            return name, pos
    for name in board_metrics:
        for tok in _metric_signals(name):
            pos = _find_all(r"\b" + re.escape(tok) + r"\b", window_lower)
            if pos:
                return name, pos
    if "accuracy" in window_lower:
        for name in board_metrics:
            if re.search(r"accuracy|percentage correct|top 1", name, re.I):
                return name, _find_all(r"\baccuracy\b", window_lower)
    return None, []


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
    """초록에서 (task, dataset, metric, value) 후보를 뽑는다.

    실데이터 1차 가동에서 확인된 오염 패턴을 게이트로 차단한다:
    - 데이터셋 언급 하나로 그 데이터셋의 모든 task 보드에 살포되는 문제
      → task 이름이 본문에 실제로 언급된 보드만 대상
    - "1.31×" 같은 배속·계수를 정확도로 오인하는 문제
      → % 표기 수치, 또는 지표 언급과 80자 이내 인접한 순수 소수만 인정
        (단위 접미 x/×/M/B가 붙은 수치와 정수는 제외)
    """
    text = f"{title or ''}. {abstract or ''}"
    lower = text.lower()
    text_norm = _norm(text)
    out = []
    seen: set[tuple] = set()
    for ds_lower, boards in index.items():
        if ds_lower not in lower:
            continue  # 빠른 사전 필터
        ds = re.escape(ds_lower)
        anchor = (rf"\bon (?:the )?{ds}\b"
                  rf"|\b{ds}[^a-z0-9]{{1,3}}(?:benchmark|dataset|"
                  rf"leaderboard|test set|val(?:idation)? set)\b")
        for m in re.finditer(anchor, lower):
            start = max(0, m.start() - WINDOW)
            window = text[start:m.end() + WINDOW]
            window_lower = window.lower()
            # 수치 후보: (데이터셋 언급까지 거리, 창 내 위치, 값, % 여부)
            nums = []
            for x in _NUM_RE.finditer(window_lower):
                raw = x.group("pct") or x.group("plain")
                nums.append((abs((start + x.start()) - m.start()),
                             x.start(), raw, bool(x.group("pct"))))
            nums.sort()
            for board in boards:
                # 논문이 그 task를 다룬다고 밝힌 보드만 — 데이터셋 살포 방지
                if _norm(board["task"]) not in text_norm:
                    continue
                metric, mentions = _match_metric(board["metrics"],
                                                 window_lower)
                if not metric:
                    continue
                key = (board["task"], board["dataset"], metric)
                if key in seen:
                    continue
                for _, pos, raw, is_pct in nums:
                    # %-없는 수치는 지표 언급 인접일 때만 (배속·계수 차단)
                    if not is_pct and not any(
                            abs(pos - mp) <= METRIC_ADJACENCY
                            for mp in mentions):
                        continue
                    if _sane(float(raw), board["metrics"][metric]):
                        seen.add(key)
                        out.append({"task": board["task"],
                                    "dataset": board["dataset"],
                                    "metric": metric, "value": raw})
                        break
    return out


def collect(conn: sqlite3.Connection) -> int:
    """수집 논문 전체를 대상으로 auto 행을 재계산한다 (stateless).

    증분(로그) 방식 대신 매 실행 전량 재추출 — 추출 규칙을 강화하면
    과거 실행이 남긴 오염 행이 다음 실행에서 자동으로 정화된다.
    텍스트 처리라 수만 편도 수십 초면 끝난다.

    주의: 삭제 범위와 재계산 범위는 반드시 일치해야 한다 — 전량 삭제 후
    일부만 재추출하면(과거 LIMIT 2000) 창 밖 논문의 행이 영구 유실된다.
    """
    removed = conn.execute(
        "DELETE FROM sota_rows WHERE source = 'auto'").rowcount
    if removed:
        print(f"[results] 기존 auto 행 {removed}건 재계산", flush=True)
    papers = conn.execute(
        """SELECT paper_url, title, abstract, date FROM papers
           WHERE source != 'archive' AND abstract IS NOT NULL
           ORDER BY date DESC"""
    ).fetchall()
    if not papers:
        print("[results] 추출 대상 신규 논문 없음", flush=True)
        conn.commit()
        return 0
    index = _benchmark_index(conn)
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
    conn.commit()
    print(f"[results] {len(papers)}편 검사, {added}행 추가", flush=True)
    return added
