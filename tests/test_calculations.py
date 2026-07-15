import pandas as pd

from src.analytics import sort_tender_details_newest_first, weighted_variance


def test_weighted_variance_excludes_zero_estimates():
    data = pd.DataFrame({"estimated_cost": [100.0, 200.0, 0.0], "quoted_value": [90.0, 240.0, 999.0]})
    assert weighted_variance(data) == 10.0


def test_tender_details_are_sorted_by_newest_available_date():
    data = pd.DataFrame([
        {"tender_id": "old", "award_date": None, "bid_opening_datetime": "2024-05-01"},
        {"tender_id": "new", "award_date": "2026-06-15", "bid_opening_datetime": "2026-05-01"},
        {"tender_id": "middle", "award_date": None, "bid_opening_datetime": "2025-07-01"},
        {"tender_id": "undated", "award_date": None, "bid_opening_datetime": None},
    ])
    result = sort_tender_details_newest_first(data)
    assert result["tender_id"].tolist() == ["new", "middle", "old", "undated"]


def test_tender_date_sort_detects_future_award_date_column_names():
    data = pd.DataFrame({"Tender ID": ["A", "B"], "Tender Award Date": ["01/04/2025", "01/04/2026"]})
    result = sort_tender_details_newest_first(data)
    assert result["Tender ID"].tolist() == ["B", "A"]
