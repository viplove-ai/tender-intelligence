from __future__ import annotations

from pathlib import Path
from typing import Any

from .cleaning import calculate_variance, clean_text
from .database import record_change, resolve_contractor, transaction, utcnow


def update_tender(
    tender_pk: int,
    changes: dict[str, Any],
    db_path: str | Path | None = None,
    source: str = "Data Correction",
    confirm_financial_change: bool = False,
) -> None:
    protected = {"tender_id", "estimated_cost", "quoted_value"}
    if protected.intersection(changes) and not confirm_financial_change:
        raise ValueError("Changing Tender ID or financial values requires explicit confirmation")
    allowed = {"region", "zone_circle", "division", "subdivision", "location", "work_type", "status", "manually_verified", "tender_id", "estimated_cost", "quoted_value"}
    unknown = set(changes) - allowed - {"contractor_name"}
    if unknown:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(unknown))}")
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_pk,)).fetchone()
        if not row:
            raise ValueError("Tender not found")
        before = dict(row)
        updates = {key: value for key, value in changes.items() if key in allowed}
        if "contractor_name" in changes:
            updates["awarded_contractor_id"] = resolve_contractor(conn, clean_text(changes["contractor_name"]))
        estimate = updates.get("estimated_cost", before["estimated_cost"])
        quote = updates.get("quoted_value", before["quoted_value"])
        updates["variance_percent"], updates["bid_position"] = calculate_variance(estimate, quote)
        updates["last_updated_at"] = utcnow()
        # Column identifiers come only from the fixed `allowed` set above; every
        # user-supplied value remains a bound SQLite parameter.
        conn.execute(
            f"UPDATE tenders SET {','.join(f'{key}=?' for key in updates)} WHERE id=?",  # nosec B608
            [*updates.values(), tender_pk],
        )
        after = dict(conn.execute("SELECT * FROM tenders WHERE id=?", (tender_pk,)).fetchone())
        record_change(conn, tender_pk, source, before, after)


def delete_tenders_by_filters(filters: dict[str, list[str]], db_path: str | Path | None = None) -> int:
    """Delete a confirmed office slice and remove contractors left with no tenders."""
    allowed = {"region", "zone_circle", "division", "subdivision"}
    clauses, parameters = [], []
    for field, values in filters.items():
        selected = [value for value in values if clean_text(value)]
        if field not in allowed:
            raise ValueError(f"Unsupported delete filter: {field}")
        if selected:
            clauses.append(f"{field} IN ({','.join('?' for _ in selected)})")
            parameters.extend(selected)
    if not clauses:
        raise ValueError("Select at least one Region, Zone/Circle, Division or Sub-division")
    with transaction(db_path) as conn:
        # `where` contains identifiers from the fixed allowlist and generated `?`
        # placeholders only. Selected values are always passed separately.
        where = " AND ".join(clauses)
        count = int(
            conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {where}", parameters).fetchone()[0]  # nosec B608
        )
        conn.execute(f"DELETE FROM tenders WHERE {where}", parameters)  # nosec B608
        conn.execute("DELETE FROM contractors WHERE id NOT IN (SELECT DISTINCT awarded_contractor_id FROM tenders WHERE awarded_contractor_id IS NOT NULL)")
        return count
