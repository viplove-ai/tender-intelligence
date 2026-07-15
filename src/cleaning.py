from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime
from typing import Any

import pandas as pd


NULL_WORDS = {"", "nan", "none", "null", "na", "n/a", "-", "--"}


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in NULL_WORDS


def clean_text(value: Any) -> str | None:
    if is_blank(value):
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_key(value: Any) -> str:
    text = clean_text(value) or ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def normalize_contractor_name(value: Any) -> str:
    text = clean_text(value) or ""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w&]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def preferred_display_name(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    if text.isupper() or text.islower():
        return text.title()
    return text


def parse_currency(value: Any) -> float | None:
    if is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None if not math.isfinite(float(value)) else float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"(?i)(inr|rs\.?|₹)", "", text)
    text = text.replace(",", "").replace(" ", "")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", ".", "-"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -abs(number) if negative else number


def parse_date(value: Any) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat(sep=" ", timespec="seconds")
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).isoformat(sep=" ", timespec="seconds")


def calculate_variance(estimated_cost: Any, quoted_value: Any) -> tuple[float | None, str]:
    estimate = parse_currency(estimated_cost)
    quote = parse_currency(quoted_value)
    if estimate is None or estimate <= 0 or quote is None:
        return None, "Not Available"
    variance = ((quote - estimate) / estimate) * 100
    if abs(variance) < 1e-9:
        return 0.0, "At Par"
    return variance, "Below" if variance < 0 else "Above"


def format_variance(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "Not Available"
    number = float(value)
    if abs(number) < 0.005:
        return "At Par"
    return f"{abs(number):.2f}% {'Below' if number < 0 else 'Above'}"


def indian_number(value: float, decimals: int = 0) -> str:
    sign = "-" if value < 0 else ""
    fixed = f"{abs(value):.{decimals}f}"
    whole, dot, fraction = fixed.partition(".")
    if len(whole) > 3:
        tail = whole[-3:]
        head = whole[:-3]
        groups = []
        while head:
            groups.append(head[-2:])
            head = head[:-2]
        whole = ",".join(reversed(groups)) + "," + tail
    return sign + whole + (dot + fraction if decimals else "")


def format_inr(value: Any, decimals: int = 0) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"₹{indian_number(float(value), decimals)}"


def format_inr_compact(value: Any, decimals: int = 2) -> str:
    """Format rupees in construction-friendly Thousand, Lakh and Crore units."""
    if value is None or pd.isna(value):
        return "—"
    number = float(value)
    absolute = abs(number)
    if absolute >= 1_00_00_000:
        return f"₹{number / 1_00_00_000:.{decimals}f} Cr"
    if absolute >= 1_00_000:
        return f"₹{number / 1_00_000:.{decimals}f} Lakh"
    if absolute >= 1_000:
        return f"₹{number / 1_000:.{decimals}f} Thousand"
    return format_inr(number, decimals=decimals)


def currency_scale(values: Any) -> tuple[float, str]:
    """Return a divisor and label suitable for a monetary chart axis."""
    numeric = pd.to_numeric(pd.Series(values), errors="coerce").abs().max()
    if pd.isna(numeric):
        return 1.0, "₹"
    if numeric >= 1_00_00_000:
        return 1_00_00_000.0, "₹ Cr"
    if numeric >= 1_00_000:
        return 1_00_000.0, "₹ Lakh"
    if numeric >= 1_000:
        return 1_000.0, "₹ Thousand"
    return 1.0, "₹"


def financial_year(date_value: Any) -> str | None:
    if is_blank(date_value):
        return None
    date = pd.to_datetime(date_value, errors="coerce")
    if pd.isna(date):
        return None
    start = date.year if date.month >= 4 else date.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def parse_publishing_office(value: Any) -> dict[str, str | None]:
    """Split a CPWD publishing-office path without discarding its original text."""
    text = clean_text(value)
    result = {"region": None, "zone_circle": None, "division": None, "subdivision": None}
    if not text:
        return result
    zone = re.search(r"\b(?:CE|SE)\b", text, flags=re.IGNORECASE)
    division = re.search(r"\bEE\b", text, flags=re.IGNORECASE)
    subdivision = re.search(r"\b(?:AE|AEE)\b", text, flags=re.IGNORECASE)
    markers = [("zone_circle", zone), ("division", division), ("subdivision", subdivision)]
    present = [(name, match) for name, match in markers if match]
    first_start = min((match.start() for _, match in present), default=len(text))
    region = text[:first_start].strip(" -")
    result["region"] = re.sub(r"(?i)^region\s*[-:]?\s*", "", region).strip() or None
    for index, (name, match) in enumerate(present):
        next_start = present[index + 1][1].start() if index + 1 < len(present) else len(text)
        result[name] = text[match.start():next_start].strip(" -") or None
    return result
