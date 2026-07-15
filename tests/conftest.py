from __future__ import annotations

import io

import pandas as pd
import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


def workbook_bytes(rows: list[dict]) -> bytes:
    output = io.BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False, engine="openpyxl")
    return output.getvalue()


@pytest.fixture
def make_workbook():
    return workbook_bytes


@pytest.fixture
def base_row():
    return {
        "Tender ID": "T-100",
        "NIT/RFP NO": "01/EE/2025-26",
        "Name of Work / Subwork / Packages": "Construction of office building",
        "Tender Publishing Office": "Delhi - CE - EE-A",
        "Estimated Cost(INR)": "1,00,00,000",
        "EMD Amount": "2,00,000",
        "Bid Submission Closing Date & Time": "15/05/2025 15:00",
        "Bid Opening Date & Time": "16/05/2025 15:30",
        "Awarded Company Name": "Manoj Kumar",
        "Quoted Value": "90,00,000",
        "Status": "LOA Issued",
    }
