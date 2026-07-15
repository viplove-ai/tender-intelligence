from __future__ import annotations

from .cleaning import clean_text


CATEGORIES = [
    "Building Construction",
    "Repair / Renovation",
    "Retaining / Protection / Drainage",
    "Road / Path / Site Development",
    "Water / Sewer / Plumbing",
    "Electrical",
    "Vehicle Hiring",
    "Manpower / Office Services",
    "Consultancy / Survey",
    "Other Civil / Miscellaneous",
]

RULES = [
    ("Vehicle Hiring", ("vehicle", "car hire", "hiring of car", "taxi")),
    ("Manpower / Office Services", ("manpower", "data entry", "office assistant", "watch and ward", "security guard", "housekeeping")),
    ("Consultancy / Survey", ("consultancy", "consultant", "survey", "soil investigation", "architectural service")),
    ("Electrical", ("electrical", "wiring", "substation", "transformer", "dg set", "street light", "hvac", "lift ", "fire alarm")),
    ("Water / Sewer / Plumbing", ("water supply", "sewer", "sewage", "plumbing", "sanitary", "pipeline", "borewell", "wtp", "stp")),
    ("Retaining / Protection / Drainage", ("retaining", "protection work", "drain", "culvert", "flood protection", "breast wall")),
    ("Road / Path / Site Development", ("road", "path", "pavement", "site development", "landscaping", "parking", "approach")),
    ("Repair / Renovation", ("repair", "renovation", "maintenance", "upgradation", "remodel", "rehabilitation", "special repair", "addition alteration")),
    ("Building Construction", ("construction of", "building", "quarters", "hostel", "office complex", "school", "hospital")),
]


def classify_work_type(description: object) -> str:
    text = (clean_text(description) or "").casefold()
    for category, keywords in RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "Other Civil / Miscellaneous"
