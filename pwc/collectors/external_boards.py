"""외부 라이브 리더보드 미러링 — Artificial Analysis / Scale SEAL.

원본 PWC 종료 후 프런티어 LLM 벤치마크(MMLU-Pro, GPQA, HLE, AIME,
LiveCodeBench …)는 논문이 아니라 평가 기관 리더보드에서 갱신된다.
해당 기관이 측정하는 벤치마크들을 보드로 미러링한다 (source='external',
UI에서 출처 배지로 구분, 각 행에 출처 링크 첨부).

- Artificial Analysis: 공식 API (무료 키 필요 — AA_API_KEY 시크릿).
  키가 없으면 안내 후 건너뜀. 출처 표기는 AA 무료 이용 조건.
- Scale SEAL: 공개 페이지의 Next.js 데이터를 최선-노력으로 파싱.
  구조 변경에 대비해 실패는 진단만 남기고 조용히 건너뛴다.

stateless 미러: 실행마다 source='external' 전체를 지우고 다시 만든다 —
외부 리더보드의 현재 상태가 곧 정답이므로 증분 관리가 필요 없다.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .. import sources

AA_API = "https://artificialanalysis.ai/api/v2/data/llms/models"
AA_LINK = [{"title": "artificialanalysis.ai",
            "url": "https://artificialanalysis.ai/"}]
SEAL_URL = "https://scale.com/leaderboard"
SEAL_LINK = [{"title": "scale.com/leaderboard",
              "url": "https://scale.com/leaderboard"}]

# AA evaluations 필드 → (task, dataset, metric). 이름은 가능한 한
# 아카이브 보드 표기와 일치시켜 기존 보드에 합류하게 한다.
AA_BENCHMARKS = {
    "mmlu_pro": ("Multi-task Language Understanding", "MMLU-Pro", "Accuracy"),
    "gpqa": ("Question Answering", "GPQA Diamond", "Accuracy"),
    "hle": ("Question Answering", "Humanity's Last Exam", "Accuracy"),
    "humanitys_last_exam": ("Question Answering", "Humanity's Last Exam",
                            "Accuracy"),
    "livecodebench": ("Code Generation", "LiveCodeBench", "Pass@1"),
    "scicode": ("Code Generation", "SciCode", "Accuracy"),
    "math_500": ("Math Word Problem Solving", "MATH-500", "Accuracy"),
    "aime": ("Math Word Problem Solving", "AIME 2025", "Accuracy"),
    "aime_25": ("Math Word Problem Solving", "AIME 2025", "Accuracy"),
    "ifbench": ("Instruction Following", "IFBench", "Accuracy"),
    "terminal_bench_hard": ("Code Generation", "Terminal-Bench Hard",
                            "Accuracy"),
    "tau2_bench_telecom": ("Task-Oriented Dialogue Systems",
                           "Tau2-Bench Telecom", "Accuracy"),
    "artificial_analysis_intelligence_index": (
        "Language Modelling", "Artificial Analysis Intelligence Index",
        "Index"),
    "artificial_analysis_coding_index": (
        "Language Modelling", "Artificial Analysis Coding Index", "Index"),
    "artificial_analysis_math_index": (
        "Language Modelling", "Artificial Analysis Math Index", "Index"),
}
# 모델 최상위 pricing 객체 → 보드. 1M 토큰당 USD 원값 유지 (백분율 아님)
AA_PRICING = {
    "price_1m_blended_3_to_1": (
        "Language Modelling", "Price per 1M Tokens (Blended 3:1)",
        "USD per 1M Tokens"),
}
# /models 비교 페이지용 추가 지표 — 값을 백분율 정규화하면 안 되는(raw)
# 비용 지표와, 필드명이 유동적인 환각률은 후보 키를 여럿 둔다
AA_RAW_BENCHMARKS = {
    "cost_per_intelligence_index_task": (
        "Language Modelling", "Cost per Intelligence Index Task",
        "Cost per task (USD)"),
    "intelligence_index_cost": (
        "Language Modelling", "Cost per Intelligence Index Task",
        "Cost per task (USD)"),
}
AA_PCT_EXTRA = {
    "aa_omniscience_hallucination_rate": (
        "Language Modelling", "AA-Omniscience Hallucination Rate",
        "Hallucination Rate"),
    "omniscience_hallucination_rate": (
        "Language Modelling", "AA-Omniscience Hallucination Rate",
        "Hallucination Rate"),
}
AA_BENCHMARKS.update(AA_PCT_EXTRA)
AA_AREA = "Natural Language Processing"


def _pct(value: object) -> str | None:
    """0~1 분수는 백분율로 정규화 (AA API는 분수, SEAL은 혼재)."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 0 <= v <= 1.2:
        v *= 100
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _insert(conn: sqlite3.Connection, task: str, dataset: str, metric: str,
            model: str, value: str, date: str | None, area: str,
            links: list) -> None:
    conn.execute(
        """INSERT INTO sota_rows
           (task, dataset, model_name, metrics, metrics_order, paper_date,
            code_links, area, source)
           VALUES (?,?,?,?,?,?,?,?, 'external')""",
        (task, dataset, model,
         json.dumps({metric: value}, ensure_ascii=False),
         json.dumps([metric]), date,
         json.dumps(links, ensure_ascii=False), area))


def collect_artificial_analysis(conn: sqlite3.Connection) -> int:
    key = os.environ.get("AA_API_KEY")
    if not key:
        print("[external] AA_API_KEY 없음 — Artificial Analysis 건너뜀 "
              "(무료 키: https://artificialanalysis.ai/api → 리포 시크릿 "
              "AA_API_KEY로 등록)", flush=True)
        return 0
    import urllib.request

    req = urllib.request.Request(
        AA_API, headers={"x-api-key": key,
                         "User-Agent": sources._USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    models = payload.get("data") or []
    added = 0
    # 환각률·태스크당 비용처럼 문서 예시에 없는 필드는 이름을 추정할 수
    # 없다 — 실제 응답의 미매핑 키를 로그로 남겨 매핑 갱신 근거로 쓴다
    unmapped: dict[str, object] = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or m.get("model_name") or m.get("slug")
        if not name:
            continue
        creator = (m.get("model_creator") or {})
        if isinstance(creator, dict) and creator.get("name"):
            name = f"{name} ({creator['name']})"
        date = (m.get("release_date") or "")[:10] or None
        evals = m.get("evaluations") or {}
        if not isinstance(evals, dict):
            continue
        for k, v in evals.items():
            if (k not in AA_BENCHMARKS and k not in AA_RAW_BENCHMARKS
                    and k not in unmapped):
                unmapped[k] = v
        seen_ds: set[str] = set()
        for key_name, spec in AA_BENCHMARKS.items():
            raw = evals.get(key_name)
            # 중첩({"value": ...}) 형태 방어
            if isinstance(raw, dict):
                raw = raw.get("value") or raw.get("score")
            value = _pct(raw)
            task, dataset, metric = spec
            if value is None or dataset in seen_ds:
                continue
            seen_ds.add(dataset)
            _insert(conn, task, dataset, metric, name, value, date,
                    AA_AREA, AA_LINK)
            added += 1
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) \
            else {}
        for key_name, spec in {**AA_RAW_BENCHMARKS, **AA_PRICING}.items():
            # 비용·가격 등 raw 지표 — evaluations/모델 최상위/pricing에서
            # 탐색, 백분율 정규화 없이 원값 유지
            raw = evals.get(key_name)
            if raw is None:
                raw = m.get(key_name)
            if raw is None:
                raw = pricing.get(key_name)
            if isinstance(raw, dict):
                raw = raw.get("value")
            try:
                v = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            task, dataset, metric = spec
            if dataset in seen_ds:
                continue
            seen_ds.add(dataset)
            _insert(conn, task, dataset, metric, name, f"{v:g}", date,
                    AA_AREA, AA_LINK)
            added += 1
    if models and isinstance(models[0], dict):
        print(f"[external] AA 모델 최상위 필드: {sorted(models[0].keys())}",
              flush=True)
        pricing = models[0].get("pricing")
        if isinstance(pricing, dict):
            print("[external] AA pricing 필드: "
                  f"{json.dumps(pricing, ensure_ascii=False)}", flush=True)
    if unmapped:
        print(f"[external] AA 미매핑 평가 키 {len(unmapped)}개: "
              f"{json.dumps(unmapped, ensure_ascii=False, default=str)[:2000]}",
              flush=True)
    print(f"[external] Artificial Analysis: 모델 {len(models)}개 → "
          f"{added}행", flush=True)
    return added


def _walk_seal(node: object, out: list, path: str = "") -> None:
    """__NEXT_DATA__ 트리에서 (리더보드명, [{model, score}...]) 후보 탐색."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("title") or node.get("slug")
        rows = node.get("results") or node.get("rankings") or node.get("rows")
        if isinstance(name, str) and isinstance(rows, list) and rows:
            parsed = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                model = (r.get("model") or r.get("model_name")
                         or r.get("name"))
                if isinstance(model, dict):
                    model = model.get("name")
                score = (r.get("score") if r.get("score") is not None
                         else r.get("rating"))
                if isinstance(model, str) and score is not None:
                    parsed.append((model, score))
            if len(parsed) >= 3:
                out.append((name, parsed))
        for v in node.values():
            _walk_seal(v, out, path)
    elif isinstance(node, list):
        for v in node:
            _walk_seal(v, out, path)


def collect_scale_seal(conn: sqlite3.Connection) -> int:
    """Scale SEAL 리더보드 최선-노력 미러 — 공개 API가 없어 페이지의
    Next.js 데이터를 파싱한다. 구조가 바뀌면 진단만 남기고 건너뛴다."""
    import re

    try:
        with sources.open_with_retry(SEAL_URL, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - 외부 소스 실패는 비치명
        print(f"[external] Scale SEAL 접근 실패 — 건너뜀: {e}", flush=True)
        return 0
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        print("[external] Scale SEAL: __NEXT_DATA__ 없음 — 페이지 구조 "
              "변경, 어댑터 갱신 필요", flush=True)
        return 0
    try:
        data = json.loads(m.group(1))
    except ValueError:
        print("[external] Scale SEAL: 데이터 파싱 실패", flush=True)
        return 0
    found: list = []
    _walk_seal(data, found)
    added = 0
    for board_name, rows in found:
        dataset = f"{board_name} (SEAL)"
        for model, score in rows:
            value = _pct(score)
            if value is None:
                continue
            _insert(conn, "Language Modelling", dataset, "Score", model,
                    value, None, AA_AREA, SEAL_LINK)
            added += 1
    if not found:
        # 다음 개선을 위한 최소 진단 — 최상위 키만
        top = list(data.keys())[:8] if isinstance(data, dict) else type(data)
        print(f"[external] Scale SEAL: 리더보드 패턴 미발견 (구조: {top})",
              flush=True)
    print(f"[external] Scale SEAL: 보드 {len(found)}개 → {added}행",
          flush=True)
    return added


def collect(conn: sqlite3.Connection) -> int:
    removed = conn.execute(
        "DELETE FROM sota_rows WHERE source = 'external'").rowcount
    if removed:
        print(f"[external] 기존 external 행 {removed}건 재계산", flush=True)
    added = 0
    for fn in (collect_artificial_analysis, collect_scale_seal):
        try:
            added += fn(conn)
        except Exception as e:  # noqa: BLE001 - 소스별 독립
            print(f"[external] {fn.__name__} 실패: {e}", flush=True)
    conn.commit()
    print(f"[external] 총 {added}행", flush=True)
    return added
