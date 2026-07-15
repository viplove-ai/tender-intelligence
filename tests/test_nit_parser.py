from pathlib import Path

import pytest

from src.nit_parser import extract_nit_pdf


def sample_pdf(name: str) -> Path:
    path = Path(__file__).parents[1] / "sample_reports" / name
    if not path.exists():
        pytest.skip(f"Sample PDF is not present: {name}")
    return path


def test_sample_nit_pdf_extracts_commercial_details_and_boq():
    path = sample_pdf("NIT+DOCUMENT-3.pdf")
    result = extract_nit_pdf(path.read_bytes(), path.name)

    assert result.page_count == 134
    assert result.nit_no == "17/EE/ACD/CPWD/Almora/2026-27(Recall)"
    assert result.estimated_cost == 2_501_353
    assert result.emd_amount == 50_027
    assert result.completion_period == "02 Months"
    assert result.submission_closing.strftime("%d-%m-%Y %H:%M") == "22-06-2026 15:00"
    assert result.bid_opening.strftime("%d-%m-%Y %H:%M") == "22-06-2026 15:30"
    assert result.performance_guarantee_percent == 5
    assert result.security_deposit_percent == 2.5
    assert result.civil_dsr_year == 2023
    assert result.civil_cost_index_percent == 38
    assert result.electrical_dsr_year is None
    assert result.electrical_cost_index_percent is None
    assert result.division == "Almora"
    assert result.location == "Pithoragarh, Distt. Pithoragarh (Uttarakhand)"
    assert len(result.boq_items) == 19
    assert sum(item["amount"] for item in result.boq_items) == result.boq_total == result.estimated_cost
    assert any("unrelated" in warning for warning in result.warnings)


def test_empty_pdf_is_rejected():
    try:
        extract_nit_pdf(b"")
    except ValueError as exc:
        assert "empty" in str(exc).lower()
    else:
        raise AssertionError("Expected an empty PDF to be rejected")


def test_pithoragarh_composite_nit_handles_unpunctuated_summary_table():
    path = sample_pdf("NITSIBPITHORAGARH.pdf")
    result = extract_nit_pdf(path.read_bytes(), path.name)

    assert result.page_count == 176
    assert result.nit_no == "22/CE/DUN/EE(ALMORA)/DEHRADUN/2024-25"
    assert result.work_name == (
        "Construction of Office -cum-residential complex at Pithoragarh under SIB "
        "Dehradun, Distt.-Pithoragarh (UK)."
    )
    assert result.estimated_cost == 53_767_593
    assert result.civil_estimated_cost == 45_521_528
    assert result.electrical_estimated_cost == 8_246_065
    assert result.emd_amount == 1_075_352
    assert result.completion_period == "15 months"
    assert result.submission_closing.strftime("%d-%m-%Y %H:%M") == "08-11-2024 17:00"
    assert result.bid_opening.strftime("%d-%m-%Y %H:%M") == "08-11-2024 17:30"
    assert result.division == "Almora"
    assert result.location == "Pithoragarh Distt.-Pithoragarh, Uttarakhand."
    assert "eligible contractors of CPWD" in result.contractor_eligibility
    assert result.performance_guarantee_percent == 5
    assert result.security_deposit_percent == 2.5
    assert result.civil_dsr_year == 2023
    assert result.civil_cost_index_percent == 26.17
    assert result.electrical_dsr_year == 2022
    assert result.electrical_cost_index_percent == 26.17
    assert len(result.boq_items) > 250
    assert result.boq_items[0]["item_no"] == "1.1.1"
    assert result.boq_items[0]["amount"] == 592_715
    assert result.boq_total == result.estimated_cost
    assert not result.warnings


def test_munsiyari_composite_nit_extracts_both_component_totals():
    path = sample_pdf("NIT+DOCUMENT.pdf")
    result = extract_nit_pdf(path.read_bytes(), path.name)

    assert result.page_count == 213
    assert result.nit_no == "33/CE/EE/ACD/CPWD/Almora/2023-24"
    assert result.work_name == (
        "Construction of Office -cum-residential complex at Munsiyari under SIB "
        "Dehradun, Distt. -Pithoragarh (UK)."
    )
    assert result.estimated_cost == 69_946_983
    assert result.civil_estimated_cost == 60_863_349
    assert result.electrical_estimated_cost == 9_083_634
    assert result.emd_amount == 1_398_940
    assert result.completion_period == "18 months"
    assert result.submission_closing.strftime("%d-%m-%Y %H:%M") == "08-03-2024 15:00"
    assert result.bid_opening.strftime("%d-%m-%Y %H:%M") == "08-03-2024 15:30"
    assert result.division == "Almora"
    assert result.location == "Munsiyari under SIB Dehradun, Distt.-Pithoragarh, Uttarakhand."
    assert "eligible contractors of CPWD" in result.contractor_eligibility
    assert result.performance_guarantee_percent == 5
    assert result.security_deposit_percent == 2.5
    assert result.civil_dsr_year == 2023
    assert result.civil_cost_index_percent == 37.38
    assert result.electrical_dsr_year == 2022
    assert result.electrical_cost_index_percent == 37.38
    assert len(result.boq_items) > 250
    assert result.boq_items[0]["item_no"] == "1.1.1"
    assert result.boq_items[0]["amount"] == 45_846
    # Civil total + electrical grand total equals the combined estimated cost.
    assert result.boq_total == 60_863_349 + 9_083_634 == result.estimated_cost
    assert not result.warnings


def test_rudraprayag_nit_extracts_summary_schedule_and_composite_total():
    path = sample_pdf("NIT17SIBR.pdf")
    result = extract_nit_pdf(path.read_bytes(), path.name)

    assert result.page_count == 238
    assert result.nit_no == "17/CE-Dehradun/EE(Garhwal)/2026-27"
    assert result.work_name == "Construction of OCR complex at Rudraprayag under SIB- Dehradun."
    assert result.estimated_cost == 66_282_553
    assert result.civil_estimated_cost == 54_761_156
    assert result.electrical_estimated_cost == 11_521_397
    assert result.emd_amount == 1_325_700
    assert result.completion_period == "12 (Twelve) Months"
    assert result.submission_closing.strftime("%d-%m-%Y %H:%M") == "24-07-2026 15:00"
    assert result.bid_opening.strftime("%d-%m-%Y %H:%M") == "24-07-2026 15:30"
    assert result.division == "Garhwal"
    assert result.location == "SIB, Rudraprayag, Uttarakhand."
    assert result.bid_type == "Percentage Rate"
    assert "eligible contractors of CPWD" in result.contractor_eligibility
    assert "Waterproofing" in result.similar_work_criteria
    assert result.performance_guarantee_percent == 5
    assert result.security_deposit_percent == 2.5
    assert result.civil_dsr_year == 2023
    assert result.civil_cost_index_percent == 31
    assert result.electrical_dsr_year == 2025
    assert result.electrical_cost_index_percent == 27.18
    assert len(result.boq_items) == 190
    assert sum(item["work_part"] == "Civil Works" for item in result.boq_items) == 126
    assert sum(item["work_part"] == "E&M Works" for item in result.boq_items) == 64
    assert result.boq_total == result.estimated_cost
    assert not result.warnings


def test_munsiyari_balance_work_nit_handles_hyphenated_office_and_boq_sections():
    path = sample_pdf("NIT+DOCUMENT-2.pdf")
    result = extract_nit_pdf(path.read_bytes(), path.name)

    assert result.page_count == 242
    assert result.nit_no == "56/CE/EE/ACD/CPWD/Almora/2025-26"
    assert result.work_name == (
        "Construction of Office-cum-residential complex at Munsiyari under SIB Dehradun, "
        "Distt.-Pithoragarh (UK). (Balance work)."
    )
    assert result.estimated_cost == 64_550_347
    assert result.civil_estimated_cost == 55_469_930
    assert result.electrical_estimated_cost == 9_080_417
    assert result.emd_amount == 1_291_100
    assert result.completion_period == "18 (Eighteen) Months"
    assert result.submission_closing.strftime("%d-%m-%Y %H:%M") == "13-03-2026 15:00"
    assert result.bid_opening.strftime("%d-%m-%Y %H:%M") == "13-03-2026 15:30"
    assert result.division == "Almora"
    assert result.location == "Munsiyari under SIB Dehradun, Distt.- Pithoragarh, Uttarakhand."
    assert result.bid_type == "Percentage Rate"
    assert "eligible contractors of CPWD" in result.contractor_eligibility
    assert "Waterproofing" in result.similar_work_criteria
    assert result.performance_guarantee_percent == 5
    assert result.security_deposit_percent == 2.5
    assert result.civil_dsr_year == 2023
    assert result.civil_cost_index_percent == 37.38
    assert result.electrical_dsr_year == 2022
    assert result.electrical_cost_index_percent == 37.38
    assert len(result.boq_items) == 304
    assert sum(item["work_part"] == "Civil Works" for item in result.boq_items) == 179
    assert sum(item["work_part"] == "E&M Works" for item in result.boq_items) == 125
    assert result.boq_total == result.estimated_cost
    assert not result.warnings
