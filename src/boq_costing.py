from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any


CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Firefighting & Fire Alarm", ("fire alarm", "fire fighting", "firefighting", "hydrant", "hose reel", "smoke detector", "heat detector", "extinguisher", "sprinkler")),
    ("Solar & Renewable Energy", ("solar", "photovoltaic", "pv module", "inverter", "dcdb")),
    ("IT, CCTV & Communications", ("cctv", "camera", "nvr", "epabx", "epbax", "telephone", "cat-6", "cat 6", "lan", "network", "data socket", "wifi", "wi-fi", "wireless access point")),
    ("HVAC & Mechanical", ("hvac", "air conditioning", "air conditioner", "split type ac", "ton capacity", "ventilation", "duct", "chiller", "ahu", "exhaust fan")),
    ("Plumbing & Sanitary", ("sanitary", "water supply", "soil pipe", "waste pipe", "sewer", "drainage", "wash basin", "water closet", "urinal", "cp brass", "g.i. pipe", "borewell", "tube well", "pump set", "nominal bore", "s.w. pipe", "hubless", "epdm rubber gasket", "lpm at")),
    ("Electrical", ("wiring", "cable", "conduit", "mccb", "mcb", "distribution board", "switch", "socket", "luminaire", "light fitting", "earthing", "lightning conductor", "electrical", "transformer", "generator", "ups", "sqmm", "led module", "geyser")),
    ("Doors, Windows & Joinery", ("door", "window", "shutter", "frame", "joinery", "cupboard", "wpc", "upvc", "aluminium glazing", "sal wood", "teak wood", "butt hinges")),
    ("Roofing & Waterproofing", ("roofing", "waterproofing", "water proofing", "water proof", "damp proof", "bitumen", "terrace treatment", "rain water", "gutter", "khurra", "sheet shall be fixed")),
    ("Flooring & Finishes", ("flooring", "tile", "granite", "marble", "plaster", "painting", "paint", "white washing", "finishing", "false ceiling", "polishing", "cladding", "new work", "two or more coats", "cement based putty", "cement primer", "kota stone")),
    ("Reinforcement & Structural Steel", ("reinforcement", "tmt", "steel bar", "structural steel", "steel work", "m.s.", "mild steel", "railing", "grating", "thermo-mechanically", "welded type tubes", "guard bar", "bars of grade")),
    ("Concrete & RCC", ("concrete", "r.c.c", "rcc", "centering", "shuttering", "form work", "cement content", "columns", "pillars", "abutments", "suspended floors", "lintels", "beams", "cantilevers", "walls (any thickness)", "area of slab", "1:2:4", "1:5:10")),
    ("Masonry", ("brick work", "brickwork", "masonry", "aac block", "stone work", "cement mortar")),
    ("Earthwork", ("earth work", "earthwork", "excavation", "excavating", "excavated", "soil", "trench", "filling", "sand filling")),
    ("External Development", ("road work", "paver", "kerb", "boundary", "fencing", "landscaping", "retaining wall", "filter media", "weep hole", "septic tank", "pvc coated")),
    ("Testing & Investigation", ("soil testing", "bearing capacity", "bore hole", "plate load", "testing laboratory", "investigation", "survey")),
]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def boq_item_key(item: dict[str, Any]) -> str:
    identity = "|".join(str(item.get(key) or "").strip().casefold() for key in ("item_no", "description", "quantity", "unit"))
    # This is a stable record key, not a password, signature, or integrity check.
    # Keep SHA-1 for backward compatibility with saved item overrides.
    return hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def classify_boq_item(item: dict[str, Any]) -> str:
    if str(item.get("item_no") or "").upper().startswith("UNALLOCATED"):
        return "Unallocated BOQ Balance"
    description = re.sub(r"\s+", " ", str(item.get("description") or "").casefold())
    unit = str(item.get("unit") or "").casefold()
    for category, keywords in CATEGORY_RULES:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(keyword.strip())}(?![a-z0-9])", description) for keyword in keywords):
            return category
    if unit == "point":
        return "Electrical"
    item_no = str(item.get("item_no") or "")
    prefix_match = re.match(r"(\d+)\.", item_no)
    if prefix_match:
        prefix_category = {
            1: "Earthwork", 2: "Concrete & RCC", 3: "Concrete & RCC", 4: "Masonry",
            5: "Roofing & Waterproofing", 6: "Doors, Windows & Joinery",
            7: "Reinforcement & Structural Steel", 8: "Flooring & Finishes",
            9: "Roofing & Waterproofing", 10: "Flooring & Finishes",
            12: "Plumbing & Sanitary", 13: "Plumbing & Sanitary", 14: "Plumbing & Sanitary",
            15: "Roofing & Waterproofing", 16: "Roofing & Waterproofing",
            17: "Plumbing & Sanitary", 18: "Doors, Windows & Joinery", 19: "External Development",
        }.get(int(prefix_match.group(1)))
        if prefix_category:
            return prefix_category
    return "Miscellaneous"


def boq_work_part(item: dict[str, Any]) -> str:
    explicit = re.sub(r"\s+", " ", str(item.get("work_part") or "")).strip()
    if explicit:
        return explicit
    category = classify_boq_item(item)
    if category in {
        "Electrical", "Firefighting & Fire Alarm", "Solar & Renewable Energy",
        "IT, CCTV & Communications", "HVAC & Mechanical",
    }:
        return "E&M Works"
    return "Civil Works"


def costing_category_key(work_part: str, category: str) -> str:
    return f"{work_part}::{category}"


def prepare_boq_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for item in items:
        amount = _number(item.get("amount")) or _number(item.get("quantity")) * _number(item.get("rate"))
        prepared.append({
            **item,
            "item_key": boq_item_key(item),
            "work_part": boq_work_part(item),
            "category": classify_boq_item(item),
            "boq_amount": amount,
        })
    return prepared


def reconcile_boq_items(
    items: list[dict[str, Any]],
    stated_boq_total: float | None,
    component_totals: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    reconciled = [dict(item) for item in items]
    usable_components = {
        str(part): _number(total) for part, total in (component_totals or {}).items() if _number(total) > 0
    }
    if usable_components:
        extracted_by_part: dict[str, float] = defaultdict(float)
        for item in reconciled:
            extracted_by_part[boq_work_part(item)] += (
                _number(item.get("amount")) or _number(item.get("quantity")) * _number(item.get("rate"))
            )
        for part, stated in usable_components.items():
            gap = stated - extracted_by_part.get(part, 0.0)
            if gap > max(1.0, stated * 0.001):
                reconciled.append({
                    "item_no": f"UNALLOCATED-{part}",
                    "description": f"Stated {part} total not represented by extracted priced rows",
                    "quantity": 1.0,
                    "unit": "Lot",
                    "rate": gap,
                    "amount": gap,
                    "work_part": part,
                })
        return reconciled
    stated = _number(stated_boq_total)
    extracted = sum(_number(item.get("amount")) or _number(item.get("quantity")) * _number(item.get("rate")) for item in items)
    gap = stated - extracted
    if stated > 0 and gap > max(1.0, stated * 0.001):
        reconciled.append({
            "item_no": "UNALLOCATED",
            "description": "Stated BOQ total not represented by extracted priced rows",
            "quantity": 1.0,
            "unit": "Lot",
            "rate": gap,
            "amount": gap,
        })
    return reconciled


def category_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"boq_amount": 0.0, "item_count": 0.0})
    for item in items:
        category = classify_boq_item(item)
        work_part = boq_work_part(item)
        amount = _number(item.get("amount")) or _number(item.get("quantity")) * _number(item.get("rate"))
        totals[(work_part, category)]["boq_amount"] += amount
        totals[(work_part, category)]["item_count"] += 1
    return [
        {"work_part": work_part, "category": category, "item_count": int(values["item_count"]), "boq_amount": values["boq_amount"]}
        for (work_part, category), values in sorted(totals.items(), key=lambda pair: pair[1]["boq_amount"], reverse=True)
    ]


def pareto_items(items: list[dict[str, Any]], threshold: float = 0.80, maximum: int = 50) -> list[dict[str, Any]]:
    prepared = prepare_boq_items(items)
    prepared.sort(key=lambda row: row["boq_amount"], reverse=True)
    total = sum(row["boq_amount"] for row in prepared)
    selected, cumulative = [], 0.0
    for row in prepared:
        if len(selected) >= maximum:
            break
        selected.append(row)
        cumulative += row["boq_amount"]
        if total and cumulative / total >= threshold:
            break
    return selected


def calculate_boq_costing(
    items: list[dict[str, Any]],
    category_percentages: dict[str, float] | None = None,
    item_overrides: dict[str, float] | None = None,
    site_overhead_percent: float = 0,
    logistics_percent: float = 0,
    contingency_percent: float = 0,
    target_profit_margin_percent: float = 0,
) -> dict[str, Any]:
    category_percentages = category_percentages or {}
    item_overrides = item_overrides or {}
    if not 0 <= target_profit_margin_percent < 100:
        raise ValueError("Target profit margin must be between 0% and 100%.")
    item_costs: list[dict[str, Any]] = []
    category_costs: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"boq_amount": 0.0, "planned_cost": 0.0})
    work_part_costs: dict[str, dict[str, float]] = defaultdict(lambda: {"boq_amount": 0.0, "planned_cost": 0.0})
    override_coverage = 0.0
    for item in items:
        key = boq_item_key(item)
        category = classify_boq_item(item)
        work_part = boq_work_part(item)
        quantity = _number(item.get("quantity"))
        boq_rate = _number(item.get("rate"))
        boq_amount = _number(item.get("amount")) or quantity * boq_rate
        percent = _number(category_percentages.get(
            costing_category_key(work_part, category), category_percentages.get(category, 100.0),
        ))
        override_rate = _number(item_overrides.get(key))
        if override_rate > 0:
            actual_rate = override_rate
            planned_cost = quantity * actual_rate
            source = "Item override"
            override_coverage += boq_amount
        else:
            actual_rate = boq_rate * percent / 100
            planned_cost = boq_amount * percent / 100
            source = f"{percent:g}% of BOQ"
        category_costs[(work_part, category)]["boq_amount"] += boq_amount
        category_costs[(work_part, category)]["planned_cost"] += planned_cost
        work_part_costs[work_part]["boq_amount"] += boq_amount
        work_part_costs[work_part]["planned_cost"] += planned_cost
        item_costs.append({
            "item_key": key, "item_no": item.get("item_no"), "description": item.get("description"),
            "work_part": work_part, "category": category, "quantity": quantity, "unit": item.get("unit"),
            "boq_rate": boq_rate, "boq_amount": boq_amount, "actual_rate": actual_rate,
            "planned_cost": planned_cost, "cost_source": source,
        })
    boq_total = sum(row["boq_amount"] for row in item_costs)
    execution_cost = sum(row["planned_cost"] for row in item_costs)
    overhead = execution_cost * _number(site_overhead_percent) / 100
    logistics = execution_cost * _number(logistics_percent) / 100
    contingency = execution_cost * _number(contingency_percent) / 100
    total_internal_cost = execution_cost + overhead + logistics + contingency
    target_bid_amount = total_internal_cost / (1 - _number(target_profit_margin_percent) / 100)
    expected_profit = target_bid_amount - total_internal_cost
    break_even_percent = ((total_internal_cost / boq_total) - 1) * 100 if boq_total else 0.0
    recommended_bid_percent = ((target_bid_amount / boq_total) - 1) * 100 if boq_total else 0.0
    categories = [{"work_part": work_part, "category": category, **values, "saving": values["boq_amount"] - values["planned_cost"]}
                  for (work_part, category), values in category_costs.items()]
    categories.sort(key=lambda row: row["boq_amount"], reverse=True)
    work_parts = [{"work_part": work_part, **values, "saving": values["boq_amount"] - values["planned_cost"]}
                  for work_part, values in work_part_costs.items()]
    work_parts.sort(key=lambda row: row["boq_amount"], reverse=True)
    return {
        "boq_total": boq_total,
        "execution_cost": execution_cost,
        "site_overhead": overhead,
        "logistics": logistics,
        "contingency": contingency,
        "total_internal_cost": total_internal_cost,
        "target_bid_amount": target_bid_amount,
        "expected_profit": expected_profit,
        "target_profit_margin_percent": _number(target_profit_margin_percent),
        "break_even_percent": break_even_percent,
        "recommended_bid_percent": recommended_bid_percent,
        "override_coverage_percent": override_coverage / boq_total * 100 if boq_total else 0.0,
        "work_parts": work_parts,
        "categories": categories,
        "items": item_costs,
    }
