import pytest

from src.boq_costing import (
    calculate_boq_costing, category_summary, classify_boq_item, costing_category_key, pareto_items,
    prepare_boq_items, reconcile_boq_items,
)


ITEMS = [
    {"item_no": "1.1", "description": "Earth work in excavation in all kinds of soil", "quantity": 10, "unit": "cum", "rate": 100, "amount": 1000},
    {"item_no": "2.1", "description": "Reinforced cement concrete M30 grade", "quantity": 5, "unit": "cum", "rate": 1000, "amount": 5000},
    {"item_no": "3.1", "description": "Wiring for light point with copper conductor", "quantity": 10, "unit": "Point", "rate": 200, "amount": 2000},
    {"item_no": "4.1", "description": "Fire alarm smoke detector", "quantity": 2, "unit": "Each", "rate": 1000, "amount": 2000},
]


def test_boq_items_are_categorised_and_summarised():
    assert classify_boq_item(ITEMS[0]) == "Earthwork"
    assert classify_boq_item(ITEMS[1]) == "Concrete & RCC"
    assert classify_boq_item(ITEMS[2]) == "Electrical"
    assert classify_boq_item(ITEMS[3]) == "Firefighting & Fire Alarm"
    summary = category_summary(ITEMS)
    assert sum(row["boq_amount"] for row in summary) == 10_000


def test_pareto_selection_stops_after_covering_eighty_percent():
    selected = pareto_items(ITEMS)
    assert [row["item_no"] for row in selected] == ["2.1", "3.1", "4.1"]
    assert sum(row["boq_amount"] for row in selected) == 9_000


def test_costing_combines_category_factors_item_overrides_and_margin():
    key = pareto_items(ITEMS)[0]["item_key"]
    result = calculate_boq_costing(
        ITEMS,
        category_percentages={"Earthwork": 80, "Electrical": 90, "Firefighting & Fire Alarm": 100, "Concrete & RCC": 90},
        item_overrides={key: 800},
        site_overhead_percent=5,
        logistics_percent=2,
        contingency_percent=3,
        target_profit_margin_percent=10,
    )
    # Raw cost: earth 800 + concrete override 4,000 + electrical 1,800 + fire 2,000.
    assert result["execution_cost"] == 8_600
    assert result["total_internal_cost"] == pytest.approx(9_460)
    assert result["target_bid_amount"] == pytest.approx(10_511.111111)
    assert result["expected_profit"] == pytest.approx(1_051.111111)
    assert result["override_coverage_percent"] == 50
    assert result["recommended_bid_percent"] == pytest.approx(5.111111)


def test_missing_extracted_value_is_kept_as_visible_unallocated_balance():
    reconciled = reconcile_boq_items(ITEMS, 12_000)
    assert reconciled[-1]["item_no"] == "UNALLOCATED"
    assert reconciled[-1]["amount"] == 2_000
    assert classify_boq_item(reconciled[-1]) == "Unallocated BOQ Balance"
    result = calculate_boq_costing(reconciled)
    assert result["boq_total"] == 12_000


def test_component_totals_create_separate_civil_and_em_balances_and_summaries():
    civil = {**ITEMS[0], "work_part": "Civil Works"}
    electrical = {**ITEMS[2], "work_part": "E&M Works"}
    reconciled = reconcile_boq_items(
        [civil, electrical], 15_000, {"Civil Works": 10_000, "E&M Works": 5_000},
    )

    balances = [item for item in reconciled if str(item["item_no"]).startswith("UNALLOCATED")]
    assert [(item["work_part"], item["amount"]) for item in balances] == [
        ("Civil Works", 9_000), ("E&M Works", 3_000),
    ]
    assert {item["work_part"] for item in prepare_boq_items(reconciled)} == {"Civil Works", "E&M Works"}
    result = calculate_boq_costing(reconciled, category_percentages={
        costing_category_key("Civil Works", "Earthwork"): 80,
        costing_category_key("E&M Works", "Electrical"): 90,
        costing_category_key("Civil Works", "Unallocated BOQ Balance"): 80,
        costing_category_key("E&M Works", "Unallocated BOQ Balance"): 90,
    })
    assert result["boq_total"] == 15_000
    assert {row["work_part"]: row["boq_amount"] for row in result["work_parts"]} == {
        "Civil Works": 10_000, "E&M Works": 5_000,
    }
    assert {row["work_part"] for row in result["categories"]} == {"Civil Works", "E&M Works"}
    assert {row["work_part"]: row["planned_cost"] for row in result["work_parts"]} == {
        "Civil Works": 8_000, "E&M Works": 4_500,
    }
