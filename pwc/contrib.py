"""커뮤니티 리더보드 기여 — contributions/*.json 검증·적재.

원본 PWC의 '결과 제출' 기능을 GitHub PR 기반으로 대체한다. 기여자는
contributions/에 JSON 파일을 추가해 PR을 올리고, CI가 스키마를 검증하며,
머지되면 일일 갱신(update-data)이 리더보드에 반영한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

REQUIRED = ("task", "dataset", "model_name", "metrics")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://")


def validate_record(record: dict, where: str) -> list[str]:
    """기여 레코드 스키마 검증. 문제 목록을 반환한다 (비면 유효)."""
    errors = []
    for field in REQUIRED:
        if not record.get(field):
            errors.append(f"{where}: 필수 필드 누락 — {field}")
    metrics = record.get("metrics")
    if metrics is not None:
        if not isinstance(metrics, dict) or not metrics:
            errors.append(f"{where}: metrics는 비어있지 않은 객체여야 합니다")
        elif not all(isinstance(k, str) and isinstance(v, (str, int, float))
                     for k, v in metrics.items()):
            errors.append(f"{where}: metrics 값은 문자열/숫자여야 합니다")
    date = record.get("paper_date")
    if date and not DATE_RE.match(str(date)):
        errors.append(f"{where}: paper_date는 YYYY-MM-DD 형식이어야 합니다")
    for url_field in ("paper_url",):
        url = record.get(url_field)
        if url and not URL_RE.match(str(url)):
            errors.append(f"{where}: {url_field}는 http(s) URL이어야 합니다")
    links = record.get("code_links")
    if links is not None:
        if not isinstance(links, list):
            errors.append(f"{where}: code_links는 배열이어야 합니다")
        else:
            for i, c in enumerate(links):
                if not (isinstance(c, dict) and URL_RE.match(str(c.get("url", "")))):
                    errors.append(f"{where}: code_links[{i}]에 유효한 url이 필요합니다")
    return errors


def load_contributions(directory: Path) -> tuple[list[dict], list[str]]:
    """디렉터리의 모든 기여 파일을 파싱·검증한다. (레코드, 오류) 반환."""
    records: list[dict] = []
    errors: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            errors.append(f"{path.name}: JSON 파싱 실패 — {e}")
            continue
        items = data if isinstance(data, list) else [data]
        for i, record in enumerate(items):
            where = f"{path.name}[{i}]"
            if not isinstance(record, dict):
                errors.append(f"{where}: 객체여야 합니다")
                continue
            errs = validate_record(record, where)
            if errs:
                errors.extend(errs)
            else:
                records.append(record)
    return records, errors


def ingest_contributions(conn: sqlite3.Connection, directory: Path) -> int:
    """검증을 통과한 기여를 sota_rows에 적재한다 (동일 항목은 멱등 스킵)."""
    if not directory.is_dir():
        return 0
    records, errors = load_contributions(directory)
    if errors:
        raise ValueError("기여 파일 검증 실패:\n" + "\n".join(errors))
    inserted = 0
    for r in records:
        exists = conn.execute(
            """SELECT 1 FROM sota_rows
               WHERE task = ? AND dataset = ? AND model_name = ?
                 AND (paper_url = ? OR (paper_url IS NULL AND ? IS NULL))""",
            (r["task"], r["dataset"], r["model_name"],
             r.get("paper_url"), r.get("paper_url")),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO sota_rows
               (task, parent_task, dataset, model_name, metrics,
                paper_url, paper_title, paper_date, code_links)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (r["task"], None, r["dataset"], r["model_name"],
             json.dumps(r["metrics"], ensure_ascii=False),
             r.get("paper_url"), r.get("paper_title"), r.get("paper_date"),
             json.dumps(r.get("code_links") or [], ensure_ascii=False)),
        )
        inserted += 1
    conn.commit()
    if inserted:
        print(f"[contrib] 커뮤니티 기여 {inserted}건 반영", flush=True)
    return inserted
