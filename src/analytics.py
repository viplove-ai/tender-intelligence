from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from .cleaning import financial_year
from .database import connect


AWARDS_WHERE = "t.awarded_contractor_id IS NOT NULL AND t.quoted_value > 0"

TENDER_DATE_PRIORITY = (
    "award_date", "tender_award_date", "awarded_date", "date_of_award", "loa_date",
    "bid_opening_datetime", "submission_closing_datetime", "publication_date",
)


def sort_tender_details_newest_first(df: pd.DataFrame) -> pd.DataFrame:
    """Sort tender-detail rows by the newest available date, keeping undated rows last."""
    if df.empty:
        return df.copy()
    named = [column for column in TENDER_DATE_PRIORITY if column in df.columns]
    generic = []
    for column in df.columns:
        parts = re.split(r"[^a-z0-9]+", str(column).casefold())
        if column not in named and ({"date", "datetime"} & set(parts)):
            generic.append(column)
    sort_key = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for column in [*named, *generic]:
        parsed = pd.to_datetime(df[column], errors="coerce", format="mixed", dayfirst=True)
        sort_key = sort_key.fillna(parsed)
    if sort_key.notna().sum() == 0:
        return df.copy()
    result = df.copy()
    result["__tender_sort_date"] = sort_key
    result = result.sort_values("__tender_sort_date", ascending=False, na_position="last", kind="stable")
    return result.drop(columns="__tender_sort_date")


def load_tenders(db_path: str | Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        df = pd.read_sql_query(
            """SELECT t.*, c.display_name AS contractor_name
               FROM tenders t LEFT JOIN contractors c ON c.id=t.awarded_contractor_id""", conn
        )
    if not df.empty:
        df["financial_year"] = df["bid_opening_datetime"].apply(financial_year)
        df["is_awarded"] = df["awarded_contractor_id"].notna() & df["quoted_value"].fillna(0).gt(0)
    return sort_tender_details_newest_first(df)


def weighted_variance(df: pd.DataFrame) -> float | None:
    if df.empty or "estimated_cost" not in df or "quoted_value" not in df:
        return None
    valid = df[df["estimated_cost"].fillna(0).gt(0) & df["quoted_value"].notna()]
    estimate = valid["estimated_cost"].sum()
    if estimate <= 0:
        return None
    return float(((valid["quoted_value"].sum() - estimate) / estimate) * 100)


def filter_tenders(df: pd.DataFrame, filters: dict[str, Any]) -> pd.DataFrame:
    result = df.copy()
    for field in ("region", "zone_circle", "division", "subdivision", "financial_year", "work_type", "contractor_name", "status", "location"):
        selected = filters.get(field)
        if selected:
            values = selected if isinstance(selected, (list, tuple, set)) else [selected]
            result = result[result[field].isin(values)]
    minimum, maximum = filters.get("estimated_cost", (None, None))
    if minimum is not None:
        result = result[result["estimated_cost"].fillna(0) >= minimum]
    if maximum is not None:
        result = result[result["estimated_cost"].fillna(0) <= maximum]
    return result


def dashboard_metrics(df: pd.DataFrame) -> dict[str, Any]:
    awards = df[df.get("is_awarded", pd.Series(False, index=df.index))] if not df.empty else df
    return {
        "unique_tenders": int(df["id"].nunique()) if not df.empty else 0,
        "awarded_tenders": int(len(awards)),
        "contractor_count": int(awards["awarded_contractor_id"].nunique()) if not awards.empty else 0,
        "total_estimated_value": float(df["estimated_cost"].sum()) if not df.empty else 0.0,
        "total_awarded_value": float(awards["quoted_value"].sum()) if not awards.empty else 0.0,
        "weighted_variance": weighted_variance(awards),
    }


def contractor_metrics(df: pd.DataFrame) -> dict[str, Any]:
    awards = df[df["estimated_cost"].notna() | df["quoted_value"].notna()].copy()
    valid = awards[awards["estimated_cost"].fillna(0).gt(0) & awards["variance_percent"].notna()]
    if awards.empty:
        return {"award_count": 0}
    below = valid[valid["variance_percent"] < 0]
    above = valid[valid["variance_percent"] > 0]
    at_par = valid[valid["variance_percent"].abs() < 1e-9]
    return {
        "award_count": int(len(awards)),
        "total_awarded_value": float(awards["quoted_value"].sum()),
        "average_award_size": float(awards["quoted_value"].mean()),
        "average_variance": float(valid["variance_percent"].mean()) if not valid.empty else None,
        "median_variance": float(valid["variance_percent"].median()) if not valid.empty else None,
        "weighted_variance": weighted_variance(valid),
        "most_below": float(valid["variance_percent"].min()) if not valid.empty else None,
        "most_above": float(valid["variance_percent"].max()) if not valid.empty else None,
        "below_count": int(len(below)), "above_count": int(len(above)), "at_par_count": int(len(at_par)),
        "divisions": sorted(awards["division"].dropna().unique().tolist()),
        "locations": awards["location"].dropna().value_counts().head(5).index.tolist(),
        "work_types": awards["work_type"].dropna().value_counts().head(5).index.tolist(),
        "minimum_tender_size": float(awards["estimated_cost"].min()) if awards["estimated_cost"].notna().any() else None,
        "maximum_tender_size": float(awards["estimated_cost"].max()) if awards["estimated_cost"].notna().any() else None,
    }


def contractor_summary(df: pd.DataFrame) -> pd.DataFrame:
    awards = df[df["is_awarded"]] if not df.empty else df
    rows = []
    for name, group in awards.groupby("contractor_name", dropna=True):
        rows.append({"Contractor": name, **contractor_metrics(group)})
    return pd.DataFrame(rows)
