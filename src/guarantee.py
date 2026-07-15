from __future__ import annotations

from typing import Any


def calculate_performance_guarantee(
    estimated_cost: float,
    bid_percent: float,
    normal_pg_percent: float = 5.0,
    additional_pg_threshold_below: float = 20.0,
    components: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    estimate = float(estimated_cost or 0)
    percentage = float(bid_percent or 0)
    normal_percent = float(normal_pg_percent or 0)
    threshold = float(additional_pg_threshold_below or 0)
    if estimate <= 0:
        raise ValueError("Estimated cost put to tender must be positive.")
    tendered_amount = estimate * (1 + percentage / 100)
    below_percent = max(0.0, -percentage)
    additional_percent = max(0.0, below_percent - threshold)
    normal_pg = estimate * normal_percent / 100
    additional_pg = estimate * additional_percent / 100
    total_percent = normal_percent + additional_percent
    total_pg = normal_pg + additional_pg
    component_pg: dict[str, float] = {}
    usable_components = {
        name: float(value) for name, value in (components or {}).items()
        if value is not None and float(value) > 0
    }
    component_total = sum(usable_components.values())
    if component_total:
        component_pg = {
            name: total_pg * value / component_total for name, value in usable_components.items()
        }
    return {
        "estimated_cost": estimate,
        "bid_percent": percentage,
        "tendered_amount": tendered_amount,
        "normal_pg_percent": normal_percent,
        "normal_pg": normal_pg,
        "additional_threshold_below_percent": threshold,
        "additional_pg_percent": additional_percent,
        "additional_pg": additional_pg,
        "total_pg_percent": total_percent,
        "total_pg": total_pg,
        "component_pg": component_pg,
    }
