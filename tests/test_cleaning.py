from src.cleaning import calculate_variance, format_inr_compact, normalize_contractor_name, parse_currency, parse_publishing_office
from src.classifier import classify_work_type


def test_indian_currency_parsing():
    assert parse_currency("₹ 3,24,29,634") == 32429634.0
    assert parse_currency("Rs. 1,25,000.50") == 125000.5


def test_compact_rupee_formatting():
    assert format_inr_compact(75_000) == "₹75.00 Thousand"
    assert format_inr_compact(12_50_000) == "₹12.50 Lakh"
    assert format_inr_compact(3_24_00_000) == "₹3.24 Cr"


def test_publishing_office_hierarchy_parsing():
    parsed = parse_publishing_office("Chandigarh - CE - Dehradun - EE-Garhwal - AE-C-4")
    assert parsed == {
        "region": "Chandigarh", "zone_circle": "CE - Dehradun",
        "division": "EE-Garhwal", "subdivision": "AE-C-4",
    }
    shimla = parse_publishing_office("Chandigarh - SE Shimla - EE-Shimla-I")
    assert shimla["zone_circle"] == "SE Shimla"
    assert shimla["division"] == "EE-Shimla-I"


def test_above_below_and_zero_estimate():
    assert calculate_variance(100, 80) == (-20.0, "Below")
    assert calculate_variance(100, 120) == (20.0, "Above")
    assert calculate_variance(0, 50) == (None, "Not Available")


def test_contractor_normalization():
    assert normalize_contractor_name("  MANOJ   KUMAR ") == normalize_contractor_name("Manoj Kumar")


def test_work_type_classification():
    assert classify_work_type("Special repair and renovation of office") == "Repair / Renovation"
    assert classify_work_type("Hiring of inspection vehicle") == "Vehicle Hiring"
