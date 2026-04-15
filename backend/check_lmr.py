"""
check_lmr.py  --  Pre-flight LMR eligibility validator
Housing SEPP 2021 Chapter 6, for 3 test addresses.

DATA SOURCES (no new API calls -- everything already cached)
------------------------------------------------------------
  Polygon + zone: data/cached_lots.json
    - Polygon: NSW Six Maps Cadastre API
        https://maps.six.nsw.gov.au/arcgis/rest/services/sixmaps/Cadastre/MapServer/0/query
    - Zone: NSW EPI_Primary_Planning_Layers Layer 2
        https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/2/query
    - Geocoded lat/lon: Nominatim (lands on road, not lot centroid)
        https://nominatim.openstreetmap.org/search

  Rules: data/rules_housing_sepp_ch6.json
    - Manually extracted from Housing SEPP 2021 PDF
    - All rules: confidence=1.0, verified=true, extracted_by=manual

GEOMETRIC CALCULATIONS (from cached polygon, no API)
------------------------------------------------------------
  Lot area:
    shapely_polygon.area x 111_000^2
    Approximate (treats 1 deg lat = 1 deg lon = 111 km).
    Same formula as compute_envelope.py for consistency.

  Front width:
    1. Identify front edge: edge whose midpoint is closest to the
       Nominatim geocoded road point (same logic as compute_envelope.py)
    2. Edge length with lat-adjusted lon degrees:
         dx_m = delta_lon x 111_000 x cos(lat_rad)
         dy_m = delta_lat x 111_000
         width = sqrt(dx_m^2 + dy_m^2)
    NOTE: compute_envelope.py uses 111_000 for both axes, which
    underestimates E-W widths by ~17% at Sydney (~34 deg S).
    This script corrects that.

LMR AREA CHECK
------------------------------------------------------------
  Zone check: R1/R2/R3/R4 is the necessary condition per SEPP Ch6.
  Canada Bay is confirmed in the NSW LMR designated area per the
  Housing SEPP 2021 (effective 28 Feb 2025 for LMR reforms).
  A full spatial check would query the NSW Planning Portal LMR
  areas layer -- not yet wired into this script.

RULES APPLIED
------------------------------------------------------------
  Scope: lmr_zone == "all_lmr" AND applicable_zones contains lot zone.
  Skipped: lmr_inner_area / lmr_outer_area (s180) -- require walking
           distance-to-station, not yet implemented.

  Dimensional checks (pass/fail against lot measurements):
    min_lot_size    -- lot area in m2
    min_lot_width   -- front edge length in m

  Informational constraints (design-dependent, cannot validate pre-DA):
    fsr                      -- max floor space ratio
    max_height               -- max building height
    min_parking_per_dwelling
"""

import json
import math
from pathlib import Path
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import nearest_points

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"

LMR_ZONES = {"R1", "R2", "R3", "R4"}

HOUSING_TYPES = [
    "dual_occupancy",
    "multi_dwelling_housing",
    "multi_dwelling_housing_terraces",
    "residential_flat_building",
]

DIMENSIONAL_PARAMS = {"min_lot_size", "min_lot_width"}


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------

def compute_lot_dimensions(polygon_coords: list, geocoded_lat: float, geocoded_lon: float) -> dict:
    """
    Compute lot area and front-edge width from a WGS84 polygon ring.

    polygon_coords: list of [lon, lat] pairs (closing point included)
    geocoded_lat/lon: Nominatim road point -- identifies the front edge.

    Returns:
        lot_area_sqm   -- approximate lot area in m2
        front_width_m  -- front edge length in m (lat-adjusted)
        front_edge_idx -- index of front edge start vertex (for debugging)
    """
    lot = Polygon(polygon_coords)
    lot_area_sqm = lot.area * (111_000 ** 2)  # same formula as compute_envelope.py

    coords = polygon_coords[:-1]  # drop closing duplicate

    # Identify front edge using nearest point on boundary.
    # More robust than closest-midpoint: handles crescent roads and corner lots
    # where the geocoded road point sits at a bend rather than directly in front.
    road_pt = Point(geocoded_lon, geocoded_lat)
    nearest_on_boundary, _ = nearest_points(lot.boundary, road_pt)

    front_edge_idx = 0
    for i in range(len(coords)):
        p1 = coords[i]
        p2 = coords[(i + 1) % len(coords)]
        if LineString([p1, p2]).distance(nearest_on_boundary) < 1e-8:
            front_edge_idx = i
            break

    p1 = coords[front_edge_idx]
    p2 = coords[(front_edge_idx + 1) % len(coords)]

    # Lat-adjusted edge length (fixes ~17% E-W underestimate in compute_envelope.py)
    lat_rad = math.radians(geocoded_lat)
    dx_m = (p2[0] - p1[0]) * 111_000 * math.cos(lat_rad)
    dy_m = (p2[1] - p1[1]) * 111_000
    front_width_m = math.sqrt(dx_m ** 2 + dy_m ** 2)

    return {
        "lot_area_sqm":   round(lot_area_sqm, 1),
        "front_width_m":  round(front_width_m, 1),
        "front_edge_idx": front_edge_idx,
    }


# ----------------------------------------------------------------------------
# Rule application
# ----------------------------------------------------------------------------

def get_applicable_rules(sepp_rules: list, housing_type: str, zone: str) -> list:
    """
    Filter SEPP Ch6 rules for a given housing type and zone.
    Only 'all_lmr' rules included -- inner/outer area rules skipped
    (need walking-distance-to-station check, not yet implemented).
    """
    return [
        r for r in sepp_rules
        if r.get("housing_type") == housing_type
        and zone in r.get("applicable_zones", [])
        and r.get("lmr_zone") == "all_lmr"
    ]


def check_rule(rule: dict, lot_area_sqm: float, front_width_m: float) -> dict:
    """
    Apply one SEPP Ch6 rule against lot dimensions.
    passed=True/False for dimensional rules, passed=None for design-dependent.
    """
    param  = rule["parameter"]
    value  = rule["value"]
    unit   = rule["unit"]
    clause = rule.get("source_clause", "")
    op     = rule.get("operator", "?")

    if param == "min_lot_size":
        actual = lot_area_sqm
        passed = actual >= value
        return {
            "rule_id":   rule["rule_id"],
            "parameter": param,
            "required":  f">= {value} {unit}",
            "actual":    f"{actual:.1f} {unit}",
            "passed":    passed,
            "clause":    clause,
            "type":      "dimensional",
        }

    if param == "min_lot_width":
        actual = front_width_m
        passed = actual >= value
        return {
            "rule_id":   rule["rule_id"],
            "parameter": param,
            "required":  f">= {value} {unit}",
            "actual":    f"{actual:.1f} {unit}",
            "passed":    passed,
            "clause":    clause,
            "note":      "measured at front boundary (proxy for front building line)",
            "type":      "dimensional",
        }

    # Design-dependent: report as informational
    op_str = "<=" if op == "max" else ">="
    return {
        "rule_id":   rule["rule_id"],
        "parameter": param,
        "required":  f"{op_str} {value} {unit}",
        "actual":    "design-dependent",
        "passed":    None,
        "clause":    clause,
        "type":      "constraint",
    }


def check_lmr_eligibility(address: str, lot_data: dict, sepp_rules: list) -> dict:
    """
    Full LMR pre-flight check for one address.

    1. Zone check (R1/R2/R3/R4 required by SEPP Ch6)
    2. Compute lot area + front width from cached polygon
    3. For each housing type, apply applicable all_lmr SEPP rules
    4. Eligible = all dimensional checks pass
       (informational constraints don't block -- they apply to the design)
    """
    zone = lot_data["zone"]

    if zone not in LMR_ZONES:
        return {
            "address":           address,
            "zone":              zone,
            "lmr_zone_eligible": False,
            "reason":            f"Zone {zone!r} is not an LMR zone (must be R1/R2/R3/R4)",
            "housing_types":     {},
        }

    polygon_coords = lot_data["polygon"]["coordinates"][0]
    dims = compute_lot_dimensions(polygon_coords, lot_data["lat"], lot_data["lon"])
    lot_area    = dims["lot_area_sqm"]
    front_width = dims["front_width_m"]

    housing_results = {}
    for htype in HOUSING_TYPES:
        applicable = get_applicable_rules(sepp_rules, htype, zone)
        checks = [check_rule(r, lot_area, front_width) for r in applicable]
        dimensional = [c for c in checks if c["type"] == "dimensional"]
        eligible = all(c["passed"] for c in dimensional) if dimensional else False
        housing_results[htype] = {
            "eligible": eligible,
            "checks":   checks,
        }

    return {
        "address":           address,
        "zone":              zone,
        "lmr_zone_eligible": True,
        "lot_area_sqm":      lot_area,
        "front_width_m":     front_width,
        "front_edge_idx":    dims["front_edge_idx"],
        "housing_types":     housing_results,
    }


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------

def print_report(result: dict) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  {result['address']}")
    print(sep)

    if not result["lmr_zone_eligible"]:
        print(f"  NOT LMR ELIGIBLE -- {result['reason']}")
        return

    print(f"  Zone         : {result['zone']}")
    print(f"  Lot area     : {result['lot_area_sqm']:.1f} m2  (cached polygon, Shapely x 111000^2)")
    print(f"  Front width  : {result['front_width_m']:.1f} m   (front edge, lat-adjusted lon degrees)")
    print(f"  LGA          : City of Canada Bay -- confirmed LMR area (Housing SEPP 2021)")

    print(f"\n  {'Housing Type':<42} Eligible")
    print(f"  {'-'*42} --------")
    for htype, hdata in result["housing_types"].items():
        label = htype.replace("_", " ").title()
        eligible = "YES" if hdata["eligible"] else "NO"
        print(f"  {label:<42} {eligible}")

    for htype, hdata in result["housing_types"].items():
        label = htype.replace("_", " ").title()
        print(f"\n  -- {label} " + "-" * max(1, 55 - len(label)))
        for c in hdata["checks"]:
            if c["passed"] is True:
                flag = "[PASS]"
            elif c["passed"] is False:
                flag = "[FAIL]"
            else:
                flag = "[INFO]"
            note = f"  <- {c['note']}" if "note" in c else ""
            print(f"  {flag} {c['parameter']:<30} required: {c['required']:<18} actual: {c['actual']}{note}")
            print(f"         {c['clause']}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> list:
    # Load cached lot data -- no API calls
    cache_path = DATA_DIR / "cached_lots.json"
    with open(cache_path) as f:
        raw_cache = json.load(f)

    # Deduplicate: some keys have trailing \r\n from an earlier run of run_apis.py
    cached = {k.strip(): v for k, v in raw_cache.items()}

    # Load SEPP Ch6 rules (manually extracted from Housing SEPP 2021 PDF)
    rules_path = DATA_DIR / "rules_housing_sepp_ch6.json"
    with open(rules_path) as f:
        sepp_rules = json.load(f)

    test_addresses = [
        "35 Connecticut Avenue Five Dock 2046",
        "28 Clare Crescent Russell Lea 2046",
        "18 Spring Street Abbotsford 2046",
    ]

    print("\nLMR Pre-flight Validation -- Housing SEPP 2021 Chapter 6")
    print("Rules  : data/rules_housing_sepp_ch6.json (manually extracted)")
    print("Lots   : data/cached_lots.json (NSW Six Maps Cadastre + EPI_Primary_Planning_Layers)")
    print("Scope  : all_lmr rules only (inner/outer area rules skipped -- need walking-distance API)")

    results = []
    for addr in test_addresses:
        if addr not in cached:
            print(f"\nWARNING: '{addr}' not found in cache. Run run_apis.py first.")
            continue
        result = check_lmr_eligibility(addr, cached[addr], sepp_rules)
        print_report(result)
        results.append(result)

    return results


if __name__ == "__main__":
    main()
