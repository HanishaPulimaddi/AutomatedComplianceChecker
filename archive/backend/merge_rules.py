"""
merge_rules.py — Merge DCP rules with Housing SEPP 2021 Ch6 overrides.

When a lot is in an LMR area, SEPP rules override DCP rules for specific
parameters. This module resolves the correct rule set for a given lot.

Key fix vs original draft:
  - Zone matching no longer hardcodes "R2"; only the passed zone and
    "all_residential" are accepted, so passing zone="R3" won't silently
    include R2-only DCP rules.
  - force_dcp_if_more_permissive flag: when True, keeps the DCP rule if
    it allows MORE than the SEPP rule (e.g. DCP height 12m > SEPP 9.5m).
    Defaults to False (SEPP always wins) which is correct for Canada Bay
    but may be wrong for other councils.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"

# SEPP parameters that can override DCP when LMR applies
SEPP_OVERRIDE_PARAMETERS = {
    "max_height",
    "max_storeys",
    "fsr",
    "min_lot_size",
    "min_lot_width",
    "min_parking_per_dwelling",
    "subdivision_min_lot_size",
    "subdivision_min_lot_width",
}

# For numeric comparisons: True means "bigger value = more permissive"
_BIGGER_IS_MORE_PERMISSIVE = {
    "max_height":   True,
    "max_storeys":  True,
    "fsr":          True,
    "min_lot_size": False,   # smaller minimum = more permissive
    "min_lot_width": False,
    "min_parking_per_dwelling": False,
    "subdivision_min_lot_size": False,
    "subdivision_min_lot_width": False,
}


def _dcp_is_more_permissive(dcp_rule: dict, sepp_rule: dict) -> bool:
    """
    Return True if the DCP rule is strictly more permissive than the SEPP rule.
    Only valid for numeric parameters in SEPP_OVERRIDE_PARAMETERS.
    """
    param = dcp_rule.get("parameter")
    if param not in _BIGGER_IS_MORE_PERMISSIVE:
        return False
    try:
        dcp_val  = float(dcp_rule["value"])
        sepp_val = float(sepp_rule["value"])
    except (KeyError, TypeError, ValueError):
        return False

    bigger_is_better = _BIGGER_IS_MORE_PERMISSIVE[param]
    if bigger_is_better:
        return dcp_val > sepp_val
    else:
        return dcp_val < sepp_val


def get_applicable_rules(
    dcp_rules: list,
    sepp_rules: list,
    zone: str,
    dwelling_type: str = "dwelling_house",
    lot_type: str = "single_frontage",
    lmr_status: str = None,
    housing_type: str = None,
    force_dcp_if_more_permissive: bool = False,
) -> dict:
    """
    Return the correct rule set for a given lot.

    Args:
        dcp_rules:    List of rules from rules_r2_canada_bay.json.
        sepp_rules:   List of rules from rules_housing_sepp_ch6.json.
        zone:         Lot's zoning code, e.g. "R2".
        dwelling_type: Proposed dwelling type, e.g. "dwelling_house".
        lot_type:     Lot configuration, e.g. "single_frontage".
        lmr_status:   None (not in LMR), "inner", or "outer".
        housing_type: Which LMR housing type is proposed (used to filter SEPP rules).
        force_dcp_if_more_permissive:
                      When True, the DCP rule wins if it is MORE permissive
                      than the SEPP rule for a given parameter.
                      Default False (SEPP always overrides) — correct for
                      Canada Bay but may need True for other councils.

    Returns:
        {
            "applied_rules": [list of rule dicts],
            "source_summary": {
                "dcp_count": int,
                "sepp_overrides": int,
                "lmr_status": str,
                "overridden_parameters": list[str],   # only when LMR applies
            }
        }
    """
    # ── Filter DCP rules ────────────────────────────────────────────────────
    # BUG FIX: do NOT hardcode "R2" in the tuple — that caused R2-only rules
    # to slip through when zone="R3" or any other zone.
    applicable_dcp = [
        r for r in dcp_rules
        if r.get("zone") in (zone, "all_residential")
        and r.get("dwelling_type") in (dwelling_type, "all")
        and r.get("lot_type") in (lot_type, "all", "not_specified")
    ]

    # ── No LMR → return DCP only ────────────────────────────────────────────
    if not lmr_status:
        return {
            "applied_rules": applicable_dcp,
            "source_summary": {
                "dcp_count": len(applicable_dcp),
                "sepp_overrides": 0,
                "lmr_status": "not in LMR area",
            },
        }

    # ── LMR applies — filter SEPP rules ─────────────────────────────────────
    applicable_sepp = [
        r for r in sepp_rules
        if zone in r.get("applicable_zones", [])
        and (
            not housing_type
            or r.get("housing_type") in (housing_type, "all_lmr_housing")
        )
        and r.get("lmr_zone") in ("all_lmr", f"lmr_{lmr_status}_area")
    ]

    # Index SEPP rules by parameter for quick lookup
    sepp_by_param: dict[str, dict] = {}
    for r in applicable_sepp:
        if r["parameter"] in SEPP_OVERRIDE_PARAMETERS:
            # If multiple SEPP rules for the same param, keep the first
            sepp_by_param.setdefault(r["parameter"], r)

    # ── Resolve overrides ────────────────────────────────────────────────────
    # For each SEPP override parameter, decide whether SEPP or DCP wins.
    effective_overrides: set[str] = set()
    kept_dcp_over_sepp: set[str] = set()

    for param, sepp_rule in sepp_by_param.items():
        if force_dcp_if_more_permissive:
            # Find the DCP rule for this parameter (if any)
            dcp_candidates = [
                r for r in applicable_dcp if r.get("parameter") == param
            ]
            if dcp_candidates and _dcp_is_more_permissive(dcp_candidates[0], sepp_rule):
                kept_dcp_over_sepp.add(param)
                continue  # DCP wins — don't override
        effective_overrides.add(param)

    # Keep DCP rules whose parameter is NOT overridden by SEPP
    non_overridden_dcp = [
        r for r in applicable_dcp
        if r["parameter"] not in effective_overrides
    ]

    # Only include SEPP rules that actually override
    overriding_sepp = [
        r for r in applicable_sepp
        if r["parameter"] in effective_overrides
    ]

    final_rules = overriding_sepp + non_overridden_dcp

    return {
        "applied_rules": final_rules,
        "source_summary": {
            "dcp_count": len(non_overridden_dcp),
            "sepp_overrides": len(overriding_sepp),
            "lmr_status": lmr_status,
            "overridden_parameters": sorted(effective_overrides),
            "dcp_kept_over_sepp": sorted(kept_dcp_over_sepp),
        },
    }


# ── CLI smoke-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    with open(DATA_DIR / "rules_r2_canada_bay.json") as f:
        dcp = json.load(f)
    with open(DATA_DIR / "rules_housing_sepp_ch6.json") as f:
        sepp = json.load(f)

    print(f"Loaded {len(dcp)} DCP rules + {len(sepp)} SEPP rules")
    print("=" * 60)

    # Test 1: NOT in LMR
    print("\nTest 1: R2 dwelling_house, NOT in LMR")
    r1 = get_applicable_rules(dcp, sepp, zone="R2",
                              dwelling_type="dwelling_house",
                              lot_type="single_frontage",
                              lmr_status=None)
    s = r1["source_summary"]
    print(f"  Rules returned : {len(r1['applied_rules'])}")
    print(f"  DCP            : {s['dcp_count']}")
    print(f"  SEPP overrides : {s['sepp_overrides']}")

    # Test 2: LMR inner, dual occupancy
    print("\nTest 2: R2 dual_occupancy, LMR INNER")
    r2 = get_applicable_rules(dcp, sepp, zone="R2",
                              dwelling_type="dual_occupancy_attached",
                              lot_type="single_frontage",
                              lmr_status="inner",
                              housing_type="dual_occupancy")
    s = r2["source_summary"]
    print(f"  Rules returned            : {len(r2['applied_rules'])}")
    print(f"  DCP (non-overridden)      : {s['dcp_count']}")
    print(f"  SEPP overrides            : {s['sepp_overrides']}")
    print(f"  Overridden parameters     : {s['overridden_parameters']}")

    height_rules = [r for r in r2["applied_rules"] if r["parameter"] == "max_height"]
    print(f"\n  Max height rules in final set:")
    for r in height_rules:
        src = "SEPP" if r.get("source_type") == "sepp_manual" else "DCP"
        print(f"    [{src}] {r['value']}m — {r.get('source_clause', 'unknown')}")

    # Test 3: Zone bug fix — R3 should NOT include R2-only DCP rules
    print("\nTest 3: Zone bug fix — R3 should not include R2-only rules")
    r3 = get_applicable_rules(dcp, sepp, zone="R3",
                              dwelling_type="dwelling_house",
                              lot_type="single_frontage",
                              lmr_status=None)
    r2_leakage = [r for r in r3["applied_rules"] if r.get("zone") == "R2"]
    print(f"  R2-zone rules in R3 result: {len(r2_leakage)}  (expected: 0)")
    print(f"  Total rules returned      : {len(r3['applied_rules'])}")
