from datetime import date, datetime

import pandas as pd

from src.analysis_store import (
    delete_tender_analysis,
    get_tender_analysis,
    list_tender_analyses,
    save_tender_analysis,
)
from src.database import connect
from src.guarantee import calculate_performance_guarantee
from src.nit_parser import NITExtraction


def sample_inputs():
    return {
        "title": "Boundary fencing analysis",
        "nit_no": "17/EE/2026-27",
        "work_description": "Boundary fencing and soil testing",
        "zone_circle": "CE - Dehradun",
        "division": "EE-Almora",
        "location": "Pithoragarh",
        "estimated_cost": 2_501_353,
        "work_type": "Building Construction",
        "bid_opening_date": date(2026, 6, 22),
    }


def sample_result():
    pg = calculate_performance_guarantee(2_501_353, -26.97)
    return {
        "confidence": "Medium",
        "comparable_count": 5,
        "strong_comparable_count": 3,
        "most_likely_percent": -7.5,
        "range_percent": (-9.0, -6.0),
        "most_likely_amount": 2_313_751.525,
        "amount_range": (2_276_231.23, 2_351_271.82),
        "comparables": pd.DataFrame({"id": [8, 9]}),
        "explanation": "Saved result",
        "planned_bid_percent": -26.97,
        "performance_guarantee": pg,
    }


def test_saved_analysis_round_trip_and_delete(db_path):
    extraction = NITExtraction(
        filename="NIT.pdf", page_count=10, nit_no="17/EE/2026-27",
        work_name="Boundary fencing and soil testing", estimated_cost=2_501_353,
        bid_opening=datetime(2026, 6, 22, 15, 30),
        boq_items=[{"item_no": "1.1", "amount": 100.0}], text="large source text",
    )
    analysis_id = save_tender_analysis(
        sample_inputs(), sample_result(), extraction, source_filename="NIT.pdf",
        costing_plan={"category_percentages": {"Earthwork": 82}, "site_overhead_percent": 5},
        db_path=db_path,
    )

    summaries = list_tender_analyses(db_path)
    assert [row["id"] for row in summaries] == [analysis_id]
    saved = get_tender_analysis(analysis_id, db_path)
    assert saved["title"] == "Boundary fencing and soil testing"
    assert saved["result"]["comparable_tender_ids"] == [8, 9]
    assert saved["result"]["planned_bid_percent"] == -26.97
    assert saved["result"]["performance_guarantee"]["bid_percent"] == -26.97
    assert saved["result"]["performance_guarantee"]["total_pg"] == calculate_performance_guarantee(2_501_353, -26.97)["total_pg"]
    assert saved["extraction"].bid_opening == datetime(2026, 6, 22, 15, 30)
    assert saved["extraction"].boq_items[0]["item_no"] == "1.1"
    assert saved["extraction"].text == ""
    assert saved["costing"]["category_percentages"]["Earthwork"] == 82
    with connect(db_path) as conn:
        assert "large source text" not in conn.execute(
            "SELECT extraction_json FROM tender_analyses WHERE id=?", (analysis_id,)
        ).fetchone()[0]

    assert delete_tender_analysis(analysis_id, db_path)
    assert get_tender_analysis(analysis_id, db_path) is None


def test_rerun_updates_saved_analysis_and_keeps_extraction(db_path):
    extraction = NITExtraction(filename="first.pdf", page_count=2, nit_no="OLD")
    analysis_id = save_tender_analysis(sample_inputs(), sample_result(), extraction, db_path=db_path)
    revised = sample_inputs()
    revised["estimated_cost"] = 3_000_000
    revised["division"] = "EE-Pithoragarh"

    same_id = save_tender_analysis(revised, sample_result(), analysis_id=analysis_id, db_path=db_path)
    saved = get_tender_analysis(same_id, db_path)

    assert same_id == analysis_id
    assert saved["estimated_cost"] == 3_000_000
    assert saved["division"] == "EE-Pithoragarh"
    assert saved["extraction"].nit_no == "OLD"
