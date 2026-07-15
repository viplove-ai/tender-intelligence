from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .classifier import classify_work_type


STOP_WORDS = {
    "and", "the", "for", "with", "from", "including", "providing", "work", "works", "construction",
    "complete", "required", "site", "under", "above", "below", "office", "land", "name", "distt",
    "district", "india", "cpwd", "miscellaneous", "maintenance", "repair", "development", "services",
    "income", "tax", "building", "uttarakhand", "pithoragarh", "himachal", "pradesh",
}


def description_similarity(left: Any, right: Any) -> float:
    """Return an inspectable 0-1 keyword similarity for two work descriptions."""
    def tokens(value: Any) -> set[str]:
        text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
        return {word for word in re.findall(r"[a-z0-9]{3,}", text) if word not in STOP_WORDS and not word.isdigit()}

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights) - 0.5 * weights
    cumulative /= weights.sum()
    return float(np.interp(quantile, cumulative, values))


def _size_band(value: float) -> int:
    if value <= 0:
        return -1
    return int(math.floor(math.log10(value) * 2))


def _cost_scale(value: float) -> str:
    if value >= 10_000_000:
        return "crore"
    if value >= 100_000:
        return "lakh"
    if value >= 1_000:
        return "thousand"
    return "rupees"


def _same_area(left: Any, right: Any) -> bool:
    a = re.sub(r"[^a-z0-9]", "", str(left or "").casefold())
    b = re.sub(r"[^a-z0-9]", "", str(right or "").casefold())
    return bool(a and b and (a == b or a.endswith(b) or b.endswith(a)))


def estimate_bid(history: pd.DataFrame, tender: dict[str, Any]) -> dict[str, Any]:
    required = {"estimated_cost", "variance_percent", "bid_opening_datetime"}
    if history.empty or not required.issubset(history.columns) or not tender.get("estimated_cost") or tender["estimated_cost"] <= 0:
        return _insufficient("A positive estimated cost and historical awards are required.")
    data = history[
        history["estimated_cost"].fillna(0).gt(0)
        & history["variance_percent"].notna()
        & history["quoted_value"].fillna(0).gt(0)
    ].copy()
    if data.empty:
        return _insufficient("No historical awards have a valid estimated cost and quoted value.")
    work_type = tender.get("work_type") or classify_work_type(tender.get("work_description"))
    work_description = tender.get("work_description") or ""
    target_cost = float(tender["estimated_cost"])
    target_cost_scale = _cost_scale(target_cost)
    data["_cost_scale"] = data["estimated_cost"].astype(float).map(_cost_scale)
    data = data[data["_cost_scale"].eq(target_cost_scale)].copy()
    if len(data) < 2:
        return _insufficient(
            f"Fewer than two historical awards were found in the {target_cost_scale} cost scale.",
            int(len(data)), data.drop(columns=["_cost_scale"]), target_cost_scale,
        )
    target_date = pd.to_datetime(tender.get("bid_opening_date") or datetime.now(), errors="coerce")
    target_date = pd.Timestamp.now() if pd.isna(target_date) else target_date
    scores, explanations, similarities, area_matches = [], [], [], []
    for _, row in data.iterrows():
        score, reasons = 0.25, [f"same {target_cost_scale} cost scale"]
        same_contractor = bool(tender.get("contractor_id") and row.get("awarded_contractor_id") == tender["contractor_id"])
        same_work = bool(work_type and row.get("work_type") == work_type)
        same_zone = _same_area(tender.get("zone_circle"), row.get("zone_circle"))
        same_division = _same_area(tender.get("division"), row.get("division"))
        if same_contractor and same_work and same_division:
            score += 8
            reasons.append("same contractor, work type and division")
        elif same_contractor and same_work:
            score += 6
            reasons.append("same contractor and work type")
        elif same_contractor and _size_band(float(row["estimated_cost"])) == _size_band(target_cost):
            score += 4
            reasons.append("same contractor and size band")
        elif same_work and same_division:
            score += 3.5
            reasons.append("same work type and division")
        elif same_work:
            score += 2
            reasons.append("same work type")
        same_location = _same_area(tender.get("location"), row.get("location"))
        if same_zone:
            score += 0.75
            reasons.append("same zone / circle")
        if same_location:
            score += 1.5
            reasons.append("same location")
        text_similarity = description_similarity(work_description, row.get("work_name"))
        if text_similarity > 0:
            score += 6.0 * text_similarity
            if text_similarity >= 0.12:
                reasons.append(f"{text_similarity:.0%} description similarity")
        ratio = min(target_cost, float(row["estimated_cost"])) / max(target_cost, float(row["estimated_cost"]))
        score += 2.5 * ratio
        reasons.append(f"{ratio:.0%} cost similarity")
        row_date = pd.to_datetime(row.get("bid_opening_datetime"), errors="coerce")
        age_years = 5.0 if pd.isna(row_date) else max(0, (target_date - row_date).days / 365.25)
        score *= math.exp(-0.12 * age_years)
        scores.append(score)
        explanations.append(", ".join(reasons))
        similarities.append(text_similarity)
        area_matches.append(same_zone or same_division or same_location)
    data["_score"] = scores
    data["_reason"] = explanations
    data["_description_similarity"] = similarities
    data["_same_area"] = area_matches
    if work_description:
        focused = data[(data["_description_similarity"] >= 0.08) | data["_same_area"]]
        if len(focused) >= 2:
            data = focused
    data = data[data["_score"] >= 1.0].sort_values("_score", ascending=False).head(50)
    if len(data) < 2:
        return _insufficient(
            f"Fewer than two suitable {target_cost_scale}-scale comparable awards were found.",
            int(len(data)), data.drop(columns=["_same_area", "_cost_scale"]), target_cost_scale,
        )
    lower, upper = data["variance_percent"].quantile([0.05, 0.95])
    data["_robust_weight"] = data["_score"] * np.where(
        data["variance_percent"].between(lower, upper), 1.0, 0.25
    )
    values = data["variance_percent"].to_numpy(float)
    weights = data["_robust_weight"].to_numpy(float)
    most_likely = weighted_quantile(values, weights, 0.5)
    low_pct = weighted_quantile(values, weights, 0.25)
    high_pct = weighted_quantile(values, weights, 0.75)
    strong = int((data["_score"] >= 4).sum())
    comparable = len(data)
    basis = strong if strong else comparable
    confidence = "High" if basis >= 10 else "Medium" if basis >= 5 else "Low"
    def amount(percent: float) -> float:
        return target_cost * (1 + percent / 100)

    return {
        "confidence": confidence, "comparable_count": int(comparable), "strong_comparable_count": strong,
        "most_likely_percent": most_likely, "range_percent": (low_pct, high_pct),
        "most_likely_amount": amount(most_likely), "amount_range": (amount(low_pct), amount(high_pct)),
        "cost_scale": target_cost_scale,
        "comparables": data.drop(columns=["_robust_weight", "_same_area", "_cost_scale"]),
        "explanation": f"Only {target_cost_scale}-scale awards were considered, then ranked by work-description similarity, work type, contractor, zone/circle, division, location, recency and estimated-cost similarity. Extreme 5% tails received one-quarter weight.",
    }


def _insufficient(reason: str, count: int = 0, records: pd.DataFrame | None = None, cost_scale: str | None = None) -> dict[str, Any]:
    return {
        "confidence": "Insufficient Data", "comparable_count": count, "strong_comparable_count": 0,
        "most_likely_percent": None, "range_percent": (None, None), "most_likely_amount": None,
        "amount_range": (None, None), "cost_scale": cost_scale,
        "comparables": records if records is not None else pd.DataFrame(), "explanation": reason,
    }
