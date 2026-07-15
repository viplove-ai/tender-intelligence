from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .database import connect, initialize_database, transaction, utcnow
from .nit_parser import NITExtraction


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def serialize_extraction(extraction: NITExtraction | None) -> str | None:
    if extraction is None:
        return None
    data = asdict(extraction)
    data.pop("text", None)  # Keep the reusable fields and BOQ, not hundreds of pages of source text.
    return json.dumps({"version": 1, "data": data}, default=_json_default)


def deserialize_extraction(raw: str | None) -> NITExtraction | None:
    if not raw:
        return None
    payload = json.loads(raw)
    data = payload.get("data", payload)
    allowed = {item.name for item in fields(NITExtraction)}
    values = {key: value for key, value in data.items() if key in allowed}
    for key in ("submission_closing", "bid_opening"):
        if values.get(key):
            values[key] = datetime.fromisoformat(values[key])
    values.setdefault("filename", "saved-analysis.pdf")
    values.setdefault("page_count", 0)
    return NITExtraction(**values)


def summarize_result(result: dict[str, Any]) -> str:
    comparables = result.get("comparables")
    comparable_ids: list[int] = []
    if isinstance(comparables, pd.DataFrame) and "id" in comparables:
        comparable_ids = [int(value) for value in comparables["id"].dropna().tolist()]
    summary = {
        "confidence": result.get("confidence"),
        "comparable_count": int(result.get("comparable_count") or 0),
        "strong_comparable_count": int(result.get("strong_comparable_count") or 0),
        "cost_scale": result.get("cost_scale"),
        "most_likely_percent": result.get("most_likely_percent"),
        "range_percent": result.get("range_percent"),
        "most_likely_amount": result.get("most_likely_amount"),
        "amount_range": result.get("amount_range"),
        "explanation": result.get("explanation"),
        "planned_bid_percent": result.get("planned_bid_percent"),
        "performance_guarantee": result.get("performance_guarantee"),
        "comparable_tender_ids": comparable_ids,
    }
    return json.dumps(summary, default=_json_default)


def list_tender_analyses(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id,title,source_filename,nit_no,work_name,zone_circle,division,location,
                      estimated_cost,work_type,bid_opening_date,created_at,updated_at,last_run_at
               FROM tender_analyses ORDER BY updated_at DESC,id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_tender_analysis(analysis_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    initialize_database(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tender_analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        return None
    record = dict(row)
    record["extraction"] = deserialize_extraction(record.pop("extraction_json"))
    result_json = record.pop("result_json")
    record["result"] = json.loads(result_json) if result_json else None
    costing_json = record.pop("costing_json")
    record["costing"] = json.loads(costing_json) if costing_json else None
    return record


def save_tender_analysis(
    inputs: dict[str, Any],
    result: dict[str, Any],
    extraction: NITExtraction | None = None,
    analysis_id: int | None = None,
    source_filename: str | None = None,
    costing_plan: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> int:
    initialize_database(db_path)
    title = str(
        inputs.get("work_description") or inputs.get("title") or inputs.get("nit_no")
        or source_filename or "Tender analysis"
    ).strip()
    if not title:
        raise ValueError("A saved analysis needs a title, NIT number, or work description.")
    opening = inputs.get("bid_opening_date")
    opening_text = opening.isoformat() if isinstance(opening, (date, datetime)) else (str(opening) if opening else None)
    now = utcnow()
    extraction_json = serialize_extraction(extraction)
    result_json = summarize_result(result)
    costing_json = json.dumps(costing_plan, default=_json_default) if costing_plan is not None else None
    with transaction(db_path) as conn:
        if analysis_id is None:
            cursor = conn.execute(
                """INSERT INTO tender_analyses(
                       title,source_filename,nit_no,work_name,zone_circle,division,location,
                       estimated_cost,work_type,bid_opening_date,extraction_json,result_json,costing_json,
                       created_at,updated_at,last_run_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    title, source_filename, inputs.get("nit_no"), inputs.get("work_description"),
                    inputs.get("zone_circle"), inputs.get("division"), inputs.get("location"),
                    float(inputs.get("estimated_cost") or 0), inputs.get("work_type"), opening_text,
                    extraction_json, result_json, costing_json, now, now, now,
                ),
            )
            return int(cursor.lastrowid)
        exists = conn.execute(
            "SELECT id,extraction_json,costing_json,source_filename FROM tender_analyses WHERE id=?", (analysis_id,)
        ).fetchone()
        if not exists:
            raise ValueError("The saved analysis no longer exists.")
        extraction_json = extraction_json or exists["extraction_json"]
        costing_json = costing_json or exists["costing_json"]
        filename = source_filename or exists["source_filename"]
        conn.execute(
            """UPDATE tender_analyses SET
                   title=?,source_filename=?,nit_no=?,work_name=?,zone_circle=?,division=?,location=?,
                   estimated_cost=?,work_type=?,bid_opening_date=?,extraction_json=?,result_json=?,costing_json=?,
                   updated_at=?,last_run_at=? WHERE id=?""",
            (
                title, filename, inputs.get("nit_no"), inputs.get("work_description"),
                inputs.get("zone_circle"), inputs.get("division"), inputs.get("location"),
                float(inputs.get("estimated_cost") or 0), inputs.get("work_type"), opening_text,
                extraction_json, result_json, costing_json, now, now, analysis_id,
            ),
        )
        return int(analysis_id)


def delete_tender_analysis(analysis_id: int, db_path: str | Path | None = None) -> bool:
    initialize_database(db_path)
    with transaction(db_path) as conn:
        cursor = conn.execute("DELETE FROM tender_analyses WHERE id=?", (analysis_id,))
        return cursor.rowcount > 0
