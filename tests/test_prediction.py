import pandas as pd

from src.prediction import description_similarity, estimate_bid


def history(count=12):
    return pd.DataFrame([
        {
            "id": i, "estimated_cost": 10_000_000 + i * 100_000, "quoted_value": (10_000_000 + i * 100_000) * (0.9 + i / 1000),
            "variance_percent": -10 + i / 10, "bid_opening_datetime": f"202{3 + i % 3}-05-01",
            "awarded_contractor_id": 1, "contractor_name": "Manoj Kumar", "work_type": "Building Construction",
            "zone_circle": "CE - Delhi", "division": "EE-A", "location": "Delhi",
        } for i in range(count)
    ])


def test_bid_estimator_sufficient():
    result = estimate_bid(history(), {"estimated_cost": 11_000_000, "contractor_id": 1, "work_type": "Building Construction", "division": "EE-A", "location": "Delhi"})
    assert result["confidence"] == "High"
    assert result["comparable_count"] == 12
    assert result["cost_scale"] == "crore"
    assert "same crore cost scale" in result["comparables"].iloc[0]["_reason"]
    assert -10 <= result["most_likely_percent"] <= -8


def test_bid_estimator_insufficient():
    result = estimate_bid(history(1), {"estimated_cost": 11_000_000, "work_type": "Electrical", "division": "Elsewhere"})
    assert result["confidence"] == "Insufficient Data"


def test_description_similarity_rewards_scope_keywords():
    target = "Boundary fencing and soil testing for office land"
    similar = "Providing boundary wall fencing and geotechnical soil testing"
    unrelated = "Hiring inspection vehicle for executive engineer"
    assert description_similarity(target, similar) > description_similarity(target, unrelated)


def test_bid_estimator_exposes_match_reason():
    data = history()
    data["work_name"] = "Boundary fencing and soil testing"
    result = estimate_bid(data, {"estimated_cost": 11_000_000, "work_description": "Boundary fencing and soil testing"})
    assert "description similarity" in result["comparables"].iloc[0]["_reason"]


def test_bid_estimator_uses_selected_zone_circle():
    result = estimate_bid(history(), {"estimated_cost": 11_000_000, "zone_circle": "CE - Delhi"})
    assert "same zone / circle" in result["comparables"].iloc[0]["_reason"]


def test_bid_estimator_only_uses_matching_indian_cost_scale():
    costs = [40_000, 80_000, 400_000, 800_000, 20_000_000, 80_000_000]
    data = pd.DataFrame([
        {
            "id": index, "estimated_cost": cost, "quoted_value": cost * 0.9,
            "variance_percent": -10, "bid_opening_datetime": "2025-05-01",
        }
        for index, cost in enumerate(costs)
    ])

    cases = [
        (60_000, "thousand", {0, 1}),
        (600_000, "lakh", {2, 3}),
        (40_000_000, "crore", {4, 5}),
    ]
    for target, scale, expected_ids in cases:
        result = estimate_bid(data, {"estimated_cost": target})
        assert result["cost_scale"] == scale
        assert result["comparable_count"] == 2
        assert set(result["comparables"]["id"]) == expected_ids
        assert all(f"same {scale} cost scale" in reason for reason in result["comparables"]["_reason"])
