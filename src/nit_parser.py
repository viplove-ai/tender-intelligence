from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pypdf import PdfReader

from .cleaning import clean_text, parse_currency


MONEY = r"(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)"
DATE = r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
TIME = r"(?<!\d)(\d{1,2}[.:]\d{2}\s*(?:(?:Hrs?\.?)|(?:AM|PM))?)"
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_EXTRACTED_TEXT_CHARS = 20_000_000


@dataclass
class NITExtraction:
    filename: str
    page_count: int
    nit_no: str | None = None
    work_name: str | None = None
    estimated_cost: float | None = None
    civil_estimated_cost: float | None = None
    electrical_estimated_cost: float | None = None
    emd_amount: float | None = None
    completion_period: str | None = None
    submission_closing: datetime | None = None
    bid_opening: datetime | None = None
    division: str | None = None
    location: str | None = None
    bid_type: str | None = None
    contractor_eligibility: str | None = None
    similar_work_criteria: str | None = None
    performance_guarantee_percent: float | None = None
    security_deposit_percent: float | None = None
    civil_dsr_year: int | None = None
    civil_cost_index_percent: float | None = None
    electrical_dsr_year: int | None = None
    electrical_cost_index_percent: float | None = None
    boq_items: list[dict[str, Any]] = field(default_factory=list)
    boq_total: float | None = None
    warnings: list[str] = field(default_factory=list)
    text: str = field(default="", repr=False)


def _compact(value: str | None) -> str | None:
    return clean_text(value)


def _first(pattern: str, text: str, flags: int = re.IGNORECASE) -> str | None:
    match = re.search(pattern, text, flags)
    return _compact(match.group(1)) if match else None


def _money(pattern: str, text: str) -> float | None:
    value = _first(pattern, text)
    return parse_currency(value)


def _parse_datetime(date_text: str | None, time_text: str | None = None) -> datetime | None:
    if not date_text:
        return None
    normalized_time = (time_text or "00:00").upper().strip()
    normalized_time = re.sub(r"\s*HRS?\.?$", "", normalized_time, flags=re.I).replace(".", ":")
    combined = f"{date_text} {normalized_time}"
    for fmt in ("%d.%m.%Y %I:%M %p", "%d.%m.%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            continue
    return None


def _extract_deadline(text: str, opening: bool = False) -> datetime | None:
    if opening:
        patterns = [
            rf"date(?:\s+and\s+time)?\s+of\s+(?:online\s+)?opening\s+of\s+bid[\s\S]{{0,120}}?{DATE}\s*(?:up\s*to|upto|at)?\s*{TIME}",
            rf"(?:time\s*&\s*date|date(?:\s+and\s+time)?)\s+of\s+opening[^\n:]*(?::|at)?\s*{TIME}\s*(?:on\s*)?{DATE}",
            rf"time\s+and\s+date\s+of\s+opening\s+of\s+bid\s*{TIME}\s*(?:on\s*)?{DATE}",
            rf"(?:bid\s+shall\s+be\s+opened|bid\s+opening)[^.\n]*?at\s*{TIME}\s+on\s+{DATE}",
            rf"date\s+of\s+opening\s*:?\s*{DATE}",
        ]
    else:
        patterns = [
            rf"last\s+date(?:\s*&\s*time)?\s+of\s+(?:online\s+)?submission\s+of\s+bid[\s\S]{{0,180}}?{DATE}\s*(?:up\s*to|upto|at)\s*{TIME}",
            rf"last\s+date\s+and\s+time\s+of\s+submission\s+of\s+bid\s*:\s*{TIME}\s+on\s+{DATE}",
            rf"last\s+date\s+of\s+online\s+submission\s+of\s+bid[\s\S]+?up\s*to\s+{TIME}\s+on\s+{DATE}",
            rf"(?:submitted|submission)[^.\n]*?(?:up\s*to|upto)\s+(?:the\s+)?{TIME}\s+on\s+{DATE}",
            rf"last\s+date\s+of\s+submission\s+of\s+bid\s*[:-]\s*{DATE}",
        ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 2:
            if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", groups[0].strip()):
                return _parse_datetime(groups[0], groups[1])
            return _parse_datetime(groups[1], groups[0])
        if len(groups) == 1:
            return _parse_datetime(groups[0])
    return None


def _extract_boq(page_texts: list[str], estimated_cost: float | None = None) -> tuple[list[dict[str, Any]], float | None]:
    starts = [
        i for i, text in enumerate(page_texts)
        if re.search(r"Schedule\s+of\s+(?:Work|Quantit(?:y|ies))\s*(?:\([^)]*\))?", text, re.I)
        and re.search(r"\b(?:Qty|Quantity)\b", text, re.I)
    ]
    if not starts:
        return [], None
    start = starts[0]
    items: list[dict[str, Any]] = []
    # Item numbers may be hierarchical (3.1.2), integers, or lettered sub-items.
    # A decimal ending in .00 followed by a unit is a quantity, not an item number.
    item_start = re.compile(r"^\s*(\d+(?:\.\d+)*|[a-z]\))\s+(.+?)\s*$", re.I)
    priced_end = re.compile(
        r"^(.+?)\s+(\d[\d,]*(?:\.\d+)?)\s+"
        r"(cum|sqm|sq\.?\s*m|kg|mtr|metre|meter|each|lot|job|point|pair|set|nos?\.?|number|hour|kWp|per\s+bag(?:\s+of\s+50\s+kg\s+cement\s+used)?)\.?\s+"
        r"(\d[\d,]*(?:\.\d+)?)\s+(?:(\d[\d,]*(?:\.\d+)?)\s+)?(₹?\s*\d[\d,]*(?:\.\d+)?)\s*$",
        re.I,
    )
    current_no: str | None = None
    description_parts: list[str] = []
    work_part = "Civil Works"

    def add_priced_item(item_no: str, match: re.Match[str]) -> None:
        description, quantity, unit, rate, _alternate_rate, amount = match.groups()
        items.append({
            "item_no": item_no,
            "description": _compact(description),
            "quantity": parse_currency(quantity),
            "unit": unit.strip().rstrip("."),
            "rate": parse_currency(rate),
            "amount": parse_currency(amount),
            "work_part": work_part,
        })

    for text in page_texts[start:]:
        if re.search(
            r"SCHEDULE\s+OF\s+(?:QUANTIT(?:Y|IES)|WORK)\s*(?:OF\s+)?\(?\s*(?:E\s*&\s*M|ELECTRICAL)\s+WORKS?",
            text, re.I,
        ):
            work_part = "E&M Works"
        elif re.search(
            r"SCHEDULE\s+OF\s+(?:QUANTIT(?:Y|IES)|WORK)\s*(?:OF\s+)?\(?\s*CIVIL\s+WORKS?",
            text, re.I,
        ):
            work_part = "Civil Works"
        for line in text.splitlines():
            line = line.strip()
            if not line or re.search(r"^(?:Corrections|Insertions|Omissions|P\s*a\s*g\s*e)\b", line, re.I):
                continue
            new_item = item_start.match(line)
            if new_item:
                token, remainder = new_item.groups()
                quantity_token = bool(re.fullmatch(r"\d+\.00", token) and re.match(
                    r"(?:cum|sqm|kg|mtr|metre|meter|each|lot|job|point|pair|set|nos?\.?|hour|kWp)\b",
                    remainder,
                    re.I,
                ))
                measurement_fragment = bool(
                    current_no and re.fullmatch(r"\d+\.\d+", token)
                    and re.match(r"(?:m|cm|mm)\s+(?:in|dia|wide|deep|long)\b", remainder, re.I)
                )
                if not quantity_token and not measurement_fragment:
                    current_no = token
                    description_parts = [remainder]
                    priced = priced_end.match(remainder)
                    if priced:
                        add_priced_item(current_no, priced)
                        current_no = None
                        description_parts = []
                    continue
            if current_no:
                description_parts.append(line)
                combined = " ".join(part for part in description_parts if part)
                priced = priced_end.match(combined)
                if priced:
                    add_priced_item(current_no, priced)
                    current_no = None
                    description_parts = []
            else:
                continue
    tail = "\n".join(page_texts[start:])
    total_values: list[float] = []
    total_patterns = [
        r"(?im)^\s*TOTAL\s*(?:₹\s*)?([\d,]+(?:\.\d{1,2})?)\s*$",
        r"(?im)^\s*GRAND\s+TOTAL\s+(?:Rs\.?|₹)\s*[:=]?\s*([\d,]+(?:\.\d{1,2})?)\s*/?\s*-?\s*$",
        r"(?im)^\s*GRAND\s+TOTAL\s*[:=]?\s*([\d,]+(?:\.\d{1,2})?)\s*$",
        r"(?im)^\s*GRAND\s+TOTAL\s+of\s+[^\n]*?\s+([\d,]+(?:\.\d{1,2})?)\s*$",
        r"(?im)^\s*GRAND\s+TOTAL\s+of\s+[^\d\n]*\n\s*([\d,]+(?:\.\d{1,2})?)\s*$",
    ]
    for pattern in total_patterns:
        for value in re.findall(pattern, tail):
            parsed = parse_currency(value)
            if parsed is not None:
                total_values.append(parsed)
    total = next((value for value in total_values if estimated_cost and abs(value - estimated_cost) <= 1), None)
    if total is None and total_values:
        combined_total = sum(dict.fromkeys(total_values))
        total = estimated_cost if estimated_cost and abs(combined_total - estimated_cost) <= 1 else total_values[-1]
    return items, total


def _extract_rate_schedules(text: str) -> list[tuple[int, float | None]]:
    schedules: list[tuple[int, float | None]] = []
    for match in re.finditer(r"Standard\s+Schedule\s+of\s+Rates", text, re.I):
        block = text[match.start():match.start() + 800]
        for discipline in ("Civil", "Electrical"):
            discipline_match = re.search(
                rf"{discipline}\s+Works?[\s\S]*?DSR\s*[- ]?\s*(20\s*\d\s*\d)[\s\S]*?Cost\s+Index\s*([\d.]+)\s*%",
                block,
                re.I,
            )
            if discipline_match:
                value = (
                    int(re.sub(r"\s+", "", discipline_match.group(1))),
                    float(discipline_match.group(2)),
                )
                if value not in schedules:
                    schedules.append(value)
        year_match = re.search(r"Delhi\s+Schedule\s+of\s+Rates\s+(20\s*\d\s*\d)", block, re.I)
        if not year_match:
            continue
        year = int(re.sub(r"\s+", "", year_match.group(1)))
        index_match = re.search(r"Cost\s+Index\s*([\d.]+)\s*%", block, re.I)
        if not index_match:
            index_match = re.search(r"([\d.]+)\s*%\s*Cost\s+Index", block, re.I)
        if not index_match:
            continue
        cost_index = float(index_match.group(1))
        value = (year, cost_index)
        if value not in schedules:
            schedules.append(value)
    return schedules


def _specialized_similar_work_criteria(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    definitions = re.findall(r"Similar\s+work\s+means\s*[“\"]\s*(.*?)[”\"]", normalized, re.I)
    unique: list[str] = []
    for definition in definitions:
        cleaned = clean_text(definition)
        if cleaned and cleaned.casefold() not in {value.casefold() for value in unique}:
            unique.append(cleaned)
    if not unique:
        return None
    return "Specialized-work definitions: " + "; ".join(unique)


def extract_nit_pdf(content: bytes, filename: str = "uploaded.pdf") -> NITExtraction:
    if not content:
        raise ValueError("The uploaded PDF is empty.")
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("The PDF exceeds the 5 MB safety limit.")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("Password-protected PDFs are not supported.") from exc
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("The PDF exceeds the 500-page safety limit.")
        page_texts: list[str] = []
        extracted_chars = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            extracted_chars += len(page_text)
            if extracted_chars > MAX_EXTRACTED_TEXT_CHARS:
                raise ValueError("The PDF contains too much extracted text to process safely.")
            page_texts.append(page_text)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The file could not be read as a PDF.") from exc
    if not any(text.strip() for text in page_texts):
        raise ValueError("No selectable text was found. This PDF may be scanned; OCR is not available yet.")

    text = "\n".join(page_texts)
    front = "\n".join(page_texts[:20])
    detail_page = next((
        page for page in page_texts[:20]
        if re.search(r"\bNIT\s+No\b", page, re.I)
        and re.search(r"Estimated\s+cost", page, re.I)
        and re.search(r"Earnest\s+Money", page, re.I)
    ), front)
    nit_no = _first(
        r"\bNIT\s*(?:No\.?|Number)\s*[:.-]?\s*(.+?)(?=\s+Name\s+of\s+Work|\n|$)",
        detail_page,
    )
    if nit_no:
        nit_no = re.sub(r"\s*/\s*", "/", nit_no).strip(" .:-")
    work_name = _first(
        r"Name\s+of\s+Work\s*:?\s*(.+?)(?=\s+(?:Location|Estimated\s+Cost|Earnest\s+money|Stipulated\s+Period|Last\s+date|$))",
        detail_page,
        re.IGNORECASE | re.DOTALL,
    )
    estimated = _money(rf"Estimated\s+Cost(?:\s+put\s+to\s+(?:bid|tender)|\s+of\s+work)?(?:\s+Total\s+Estimated\s+cost)?\s*:?\s*{MONEY}", detail_page)
    total_after_amount = _money(rf"{MONEY}\s*/?\s*-?\s*\(\s*Total\s*\)", detail_page)
    if total_after_amount:
        estimated = total_after_amount
    civil_estimated = _money(rf"Civil\s+Work\s*:\s*{MONEY}", detail_page)
    if not civil_estimated:
        civil_estimated = _money(rf"{MONEY}\s*/?\s*-?\s*\(\s*Civil\s+Works?\s*\)", detail_page)
    electrical_estimated = _money(rf"Electrical\s+Work\s*:\s*{MONEY}", detail_page)
    if not electrical_estimated:
        electrical_estimated = _money(rf"{MONEY}\s*/?\s*-?\s*\(\s*(?:Electrical|E\s*&\s*M)\s+Works?\s*\)", detail_page)
    emd = _money(rf"(?:Earnest\s+Money(?:\s+Deposit)?|Amount\s+of\s+Earnest\s+Money\s+Deposit)\s*:?\s*{MONEY}", detail_page)
    completion = _first(
        r"(?:Stipulated\s+Period\s+of\s+Completion(?:\s+of\s+work)?|Period\s+of\s+completion|Time\s+allowed(?:\s+for\s+completion\s+of\s+work)?)\s*:?\s*([0-9]+\s*(?:\([^)]*\)\s*)?(?:days?|months?|years?))",
        detail_page,
    )
    division = _first(r"(?:OFFICE\s+OF\s+)?EXECUTIVE\s+ENGINEER\s*[-–]\s*([^\n,]+)", detail_page)
    if not division:
        # Prefer the inviting authority. Generic matches also see signature labels
        # such as "Executive Engineer (C)", where C denotes Civil, not a division.
        division = _first(r"The\s+EXECUTIVE\s+ENGINEER\s*\(([^)]+)\)", front)
    if not division:
        division = _first(r"The\s+EXECUTIVE\s+ENGINEER\s*[-–]\s*([^,\n]+)", front)
    location = _first(r"\bLocation\s*:?\s*(.+?)(?=\s+Estimated\s+cost)", detail_page, re.I | re.S)
    if not location:
        location = _first(r"\bLocation\s*:?\s*(.+?)(?=\s+Estimated\s+cost)", front, re.I | re.S)
    if location and (len(location) > 200 or re.search(r"Estimated\s+cost|Earnest\s+Money|Period\s+of\s+Completion", location, re.I)):
        location = None
    if not location:
        location = _first(r"\bat\s+(.+?)(?=\.\s*$)", work_name or "")
    bid_type = "Percentage Rate" if re.search(r"Percentage\s+rate\s+(?:composite\s+)?bids?|PERCENTAGE\s+RATE\s+TENDER", front, re.I) else None
    eligibility = _first(r"(Only\s+the\s+enlisted\s+contractors\s+of\s+Class.+?)(?=\n\s*2\.)", front, re.I | re.DOTALL)
    if not eligibility:
        eligibility = _first(
            r"bids\s+from\s+(.+?eligible\s+contractors\s+of\s+CPWD.+?)(?=\s*for\s+the\s+following\s+work)",
            front,
            re.I | re.S,
        )
    similar = _first(r"(The\s+Contractor\s+should\s+have\s+satisfactorily\s+completed.+?)(?=\n\s*1\.2\.2|\n\s*Online\s+bid|$)", text, re.I | re.DOTALL)
    if not similar:
        similar = _specialized_similar_work_criteria(front)
    performance = _money(r"Performance\s+Guarantee(?:\s*\([a-z]\))?(?:\s+of)?\s+(\d+(?:\.\d+)?)\s*%", text)
    security = _money(r"Security\s+Deposit\s+(\d+(?:\.\d+)?)\s*%", text)
    rate_schedules = _extract_rate_schedules(text)
    civil_dsr_year, civil_cost_index = rate_schedules[0] if rate_schedules else (None, None)
    electrical_dsr_year, electrical_cost_index = rate_schedules[1] if len(rate_schedules) > 1 else (None, None)
    boq_items, boq_total = _extract_boq(page_texts, estimated)
    warnings: list[str] = []
    if estimated and boq_total and abs(estimated - boq_total) > 1:
        warnings.append("The BOQ total does not match the stated estimated cost.")
    if not boq_items:
        warnings.append("No priced BOQ rows were detected; verify the Schedule of Quantities manually.")
    work_words = set(re.findall(r"[a-z]{4,}", (work_name or "").casefold()))
    criteria_words = set(re.findall(r"[a-z]{4,}", (similar or "").casefold()))
    suspicious = {"housekeeping", "caretaking", "ward", "vehicle", "manpower"} & criteria_words
    if suspicious and not (suspicious & work_words):
        warnings.append("The similar-work eligibility appears unrelated to the tender scope and may be a copied clause.")
    if not nit_no:
        warnings.append("NIT number was not detected.")
    if not estimated:
        warnings.append("Estimated cost was not detected.")

    return NITExtraction(
        filename=filename,
        page_count=len(page_texts),
        nit_no=nit_no,
        work_name=work_name,
        estimated_cost=estimated,
        civil_estimated_cost=civil_estimated,
        electrical_estimated_cost=electrical_estimated,
        emd_amount=emd,
        completion_period=completion,
        submission_closing=_extract_deadline(front),
        bid_opening=_extract_deadline(front, opening=True),
        division=division,
        location=location,
        bid_type=bid_type,
        contractor_eligibility=eligibility,
        similar_work_criteria=similar,
        performance_guarantee_percent=performance,
        security_deposit_percent=security,
        civil_dsr_year=civil_dsr_year,
        civil_cost_index_percent=civil_cost_index,
        electrical_dsr_year=electrical_dsr_year,
        electrical_cost_index_percent=electrical_cost_index,
        boq_items=boq_items,
        boq_total=boq_total,
        warnings=warnings,
        text=text,
    )
