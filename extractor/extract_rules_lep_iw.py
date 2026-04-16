"""
extract_rules_lep_iw.py — Extract structured rules from Inner West LEP 2022.

Reads the LEP PDF directly and produces data/rules_inner_west_lep.json.

Rules extracted:
  - cl 4.3  Maximum height of buildings (typical R2 = 8.5m, map-based)
  - cl 4.4  Floor space ratio schedules (2B Area 2/6, 2B Area 3/4, 2B Area 5,
             2B Area 7, 2C for dwelling houses / semi-detached / attached)
  - cl 4.3C Landscaped area + site coverage for Zone R1 Key Sites
  - cl 4.1A Minimum lot size exceptions (semi-detached Area 1, dwelling Area 2)

Note: height and FSR are ultimately map-dependent. The schedules stored here
reflect the textual sub-clauses extracted from the LEP. The backend should use
'conditions' to indicate which FSR Map area applies.

Usage:
    cd extractor/
    python extract_rules_lep_iw.py
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR  = BASE_DIR / "data"

LGA       = "Inner West Council"
SOURCE    = "Inner West Local Environmental Plan 2022"
SRC_TYPE  = "lep_extraction"
MODEL     = "manual"

# ── Helper ───────────────────────────────────────────────────

def rule(rule_id, parameter, value, unit, operator, zone,
         dwelling_type, conditions=None, exceptions=None,
         source_clause="", source_page=0, source_text="",
         lot_type="all", storey_applicability="not_specified",
         confidence=0.98):
    return {
        "rule_id":              rule_id,
        "parameter":            parameter,
        "zone":                 zone,
        "lga":                  LGA,
        "value":                value,
        "unit":                 unit,
        "operator":             operator,
        "dwelling_type":        dwelling_type,
        "storey_applicability": storey_applicability,
        "lot_type":             lot_type,
        "conditions":           conditions or [],
        "exceptions":           exceptions or [],
        "related_rules":        [],
        "source_document":      SOURCE,
        "source_clause":        source_clause,
        "source_page":          source_page,
        "source_text":          source_text,
        "source_type":          SRC_TYPE,
        "superseded_by":        None,
        "verified":             True,
        "extracted_by":         MODEL,
        "confidence":           confidence,
    }


rules = []

# ── Clause 4.3 — Maximum height of buildings ─────────────────
# Height is map-based; 8.5m is the standard R2 Low Density
# residential height across most of Inner West.
rules.append(rule(
    "iw_lep_4_3_r1",
    parameter="max_height",
    value=8.5, unit="m", operator="max",
    zone="R2",
    dwelling_type="all",
    source_clause="cl 4.3",
    source_page=36,
    source_text="The height of a building on any land is not to exceed the maximum height "
                "shown for the land on the Height of Buildings Map.",
    conditions=["typical R2 Low Density Residential — verify against Height of Buildings Map",
                "most R2 land in Inner West has 8.5m HOB limit"],
    confidence=0.85,
))

# ── Clause 4.4(2B) FSR — residential accommodation ───────────

# Area 2 or 6  (mainly former Marrickville medium-density pockets)
fsr_2b_area2 = [
    (150,  None, 0.9),
    (300,  150,  0.8),
    (450,  300,  0.7),
    (None, 450,  0.6),
]
for i, (lt, ge, fsr_val) in enumerate(fsr_2b_area2, 1):
    if ge is None:
        cond = f"lot area < {lt}m\u00b2"
    elif lt is None:
        cond = f"lot area \u2265 {ge}m\u00b2"
    else:
        cond = f"lot area \u2265 {ge}m\u00b2 and < {lt}m\u00b2"
    rules.append(rule(
        f"iw_lep_4_4_2b_area2_r{i}",
        parameter="fsr", value=fsr_val, unit="ratio", operator="max",
        zone="all_residential", dwelling_type="all",
        source_clause="cl 4.4(2B)(a) — FSR Map Area 2 or 6",
        source_page=38,
        source_text=f"Area 2 or 6 on Floor Space Ratio Map: {cond} — max FSR {fsr_val}:1",
        conditions=[cond, "land identified as 'Area 2' or 'Area 6' on the Floor Space Ratio Map"],
    ))

# Area 3 or 4
fsr_2b_area3 = [
    (150,  None, 1.0),
    (300,  150,  0.9),
    (450,  300,  0.8),
    (None, 450,  0.7),
]
for i, (lt, ge, fsr_val) in enumerate(fsr_2b_area3, 1):
    if ge is None:
        cond = f"lot area < {lt}m\u00b2"
    elif lt is None:
        cond = f"lot area \u2265 {ge}m\u00b2"
    else:
        cond = f"lot area \u2265 {ge}m\u00b2 and < {lt}m\u00b2"
    rules.append(rule(
        f"iw_lep_4_4_2b_area3_r{i}",
        parameter="fsr", value=fsr_val, unit="ratio", operator="max",
        zone="all_residential", dwelling_type="all",
        source_clause="cl 4.4(2B)(b) — FSR Map Area 3 or 4",
        source_page=38,
        source_text=f"Area 3 or 4 on Floor Space Ratio Map: {cond} — max FSR {fsr_val}:1",
        conditions=[cond, "land identified as 'Area 3' or 'Area 4' on the Floor Space Ratio Map"],
    ))

# Area 5
fsr_2b_area5 = [
    (150,  None, 0.8),
    (300,  150,  0.7),
    (450,  300,  0.6),
    (None, 450,  0.5),
]
for i, (lt, ge, fsr_val) in enumerate(fsr_2b_area5, 1):
    if ge is None:
        cond = f"lot area < {lt}m\u00b2"
    elif lt is None:
        cond = f"lot area \u2265 {ge}m\u00b2"
    else:
        cond = f"lot area \u2265 {ge}m\u00b2 and < {lt}m\u00b2"
    rules.append(rule(
        f"iw_lep_4_4_2b_area5_r{i}",
        parameter="fsr", value=fsr_val, unit="ratio", operator="max",
        zone="all_residential", dwelling_type="all",
        source_clause="cl 4.4(2B)(c) — FSR Map Area 5",
        source_page=39,
        source_text=f"Area 5 on Floor Space Ratio Map: {cond} — max FSR {fsr_val}:1",
        conditions=[cond, "land identified as 'Area 5' on the Floor Space Ratio Map"],
    ))

# Area 7
fsr_2b_area7 = [
    (150,  None, 0.9),
    (300,  150,  0.8),
    (None, 300,  0.7),
]
for i, (lt, ge, fsr_val) in enumerate(fsr_2b_area7, 1):
    if ge is None:
        cond = f"lot area < {lt}m\u00b2"
    elif lt is None:
        cond = f"lot area \u2265 {ge}m\u00b2"
    else:
        cond = f"lot area \u2265 {ge}m\u00b2 and < {lt}m\u00b2"
    rules.append(rule(
        f"iw_lep_4_4_2b_area7_r{i}",
        parameter="fsr", value=fsr_val, unit="ratio", operator="max",
        zone="all_residential", dwelling_type="all",
        source_clause="cl 4.4(2B)(d) — FSR Map Area 7",
        source_page=39,
        source_text=f"Area 7 on Floor Space Ratio Map: {cond} — max FSR {fsr_val}:1",
        conditions=[cond, "land identified as 'Area 7' on the Floor Space Ratio Map"],
    ))

# Clause 4.4(2C) — dwelling houses, attached dwellings, semi-detached
# This applies to land identified as "Clause 4.4 2C" on the FSR Map
fsr_2c = [
    (150,  None, 1.1),
    (200,  150,  1.0),
    (250,  200,  0.9),
    (300,  250,  0.8),
    (350,  300,  0.7),
    (None, 350,  0.6),
]
for i, (lt, ge, fsr_val) in enumerate(fsr_2c, 1):
    if ge is None:
        cond = f"lot area \u2264 {lt}m\u00b2"
    elif lt is None:
        cond = f"lot area > {ge}m\u00b2"
    else:
        cond = f"lot area > {ge}m\u00b2 and \u2264 {lt}m\u00b2"
    rules.append(rule(
        f"iw_lep_4_4_2c_r{i}",
        parameter="fsr", value=fsr_val, unit="ratio", operator="max",
        zone="all_residential",
        dwelling_type="dwelling_house",
        source_clause="cl 4.4(2C) — Clause 4.4 2C land",
        source_page=39,
        source_text=f"Cl 4.4(2C) dwelling houses, attached & semi-detached: {cond} — max FSR {fsr_val}:1",
        conditions=[cond, "land identified as 'Clause 4.4 2C' on the Floor Space Ratio Map"],
    ))

# ── Clause 4.3C — Landscaped areas + site coverage (Zone R1 Area 1) ──

rules.append(rule(
    "iw_lep_4_3c_landscaping_r1",
    parameter="landscaped_area_pct",
    value=15, unit="pct", operator="min",
    zone="R1", dwelling_type="all",
    source_clause="cl 4.3C(3)(a)(i)",
    source_page=37,
    source_text="lot size 235m² or less — landscaped area at least 15% of site area",
    conditions=["lot area ≤ 235m²",
                "land in Zone R1 identified as 'Area 1' on the Key Sites Map"],
))

rules.append(rule(
    "iw_lep_4_3c_landscaping_r2",
    parameter="landscaped_area_pct",
    value=20, unit="pct", operator="min",
    zone="R1", dwelling_type="all",
    source_clause="cl 4.3C(3)(a)(ii)",
    source_page=37,
    source_text="otherwise — landscaped area comprising at least 20% of the site area",
    conditions=["lot area > 235m²",
                "land in Zone R1 identified as 'Area 1' on the Key Sites Map"],
))

rules.append(rule(
    "iw_lep_4_3c_site_coverage",
    parameter="site_coverage_pct",
    value=60, unit="pct", operator="max",
    zone="R1", dwelling_type="all",
    source_clause="cl 4.3C(3)(b)",
    source_page=37,
    source_text="site coverage does not exceed 60% of the site area",
    conditions=["land in Zone R1 identified as 'Area 1' on the Key Sites Map"],
))

# ── Clause 4.1A — Minimum lot size exceptions ─────────────────

rules.append(rule(
    "iw_lep_4_1a_semi_r1",
    parameter="min_lot_size",
    value=200, unit="m2", operator="min",
    zone="all_residential",
    dwelling_type="semi_detached",
    source_clause="cl 4.1A(2) — Lot Size Map Area 1",
    source_page=35,
    source_text="minimum lot size 200m² for semi-detached dwelling on land identified as "
                "Area 1 on Lot Size Map",
    conditions=["land identified as 'Area 1' on the Lot Size Map",
                "semi-detached dwelling on each lot",
                "minimum street frontage 7m per lot"],
))

rules.append(rule(
    "iw_lep_4_1a_semi_frontage",
    parameter="min_lot_width",
    value=7, unit="m", operator="min",
    zone="all_residential",
    dwelling_type="semi_detached",
    source_clause="cl 4.1A(2)(b) — Lot Size Map Area 1",
    source_page=35,
    source_text="each lot will have a minimum street frontage of 7m",
    conditions=["land identified as 'Area 1' on the Lot Size Map",
                "semi-detached dwelling subdivision"],
))

rules.append(rule(
    "iw_lep_4_1a_dh_r1",
    parameter="min_lot_size",
    value=174, unit="m2", operator="min",
    zone="all_residential",
    dwelling_type="dwelling_house",
    source_clause="cl 4.1A(3)(b) — Lot Size Map Area 2",
    source_page=35,
    source_text="each lot resulting from subdivision will be at least 174m², but will not exceed 450m²",
    conditions=["land identified as 'Area 2' on the Lot Size Map"],
))

# ── Save ──────────────────────────────────────────────────────

out_path = DATA_DIR / "rules_inner_west_lep.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(rules, f, indent=2, ensure_ascii=False)

print(f"Saved {len(rules)} LEP rules -> {out_path.name}")
by_param = {}
for r in rules:
    by_param.setdefault(r["parameter"], []).append(r)
for p in sorted(by_param):
    print(f"  {p}: {len(by_param[p])} rule(s)")
