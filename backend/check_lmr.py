"""
check_lmr.py  --  LMR eligibility validator
Housing SEPP 2021 Chapter 6

DATA SOURCES
------------------------------------------------------------
  Polygon + zone: data/cached_lots.json
    - Polygon: NSW Six Maps Cadastre API
    - Zone: NSW EPI_Primary_Planning_Layers Layer 2
    - Geocoded lat/lon: Nominatim (lands on road, not lot centroid)

  Town Centres: data/town_centres.geojson  (or live NSW API fallback)
    - NSW SEPP_Housing_2021 MapServer Layer 6
    - 59 town centre polygons covering Sydney metro area
    - Fetched 2026-04-15; refresh with fetch_town_centres() as needed

  Rules: data/rules_housing_sepp_ch6.json
    - Manually extracted from Housing SEPP 2021 PDF
    - confidence=1.0, verified=true, extracted_by=manual

LMR AREA CHECK
------------------------------------------------------------
  Zone check: R1/R2/R3/R4 required per SEPP Ch6 s161.
  Spatial check: nearest-boundary distance from lot centroid to
    each town centre polygon from the NSW Town Centres Map.
    Walking distance = straight-line × WALKING_FACTOR (1.35).
    Inner area:  walking distance < 400 m
    Outer area:  400 m <= walking distance < 800 m
    Not in LMR:  walking distance >= 800 m (all town centres)

  Note: Schedule 11 stations (train/metro/light rail) are a second
  trigger for LMR eligibility. This script uses town centres only.
  Add station polygons to data/town_centres.geojson to extend coverage.

RULES APPLIED
------------------------------------------------------------
  all_lmr rules apply to both inner and outer lots.
  lmr_inner_area / lmr_outer_area rules (s180 height/FSR tiers)
  are filtered in based on actual spatial classification.

  Dimensional checks (pass/fail against lot measurements):
    min_lot_size    -- lot area in m2
    min_lot_width   -- front edge length in m

  Informational constraints (design-dependent, cannot validate pre-DA):
    fsr / max_height / min_parking_per_dwelling
"""

import json
import math
import requests
from pathlib import Path
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import nearest_points

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR  = BASE_DIR.parent / "data"

LMR_ZONES = {"R1", "R2", "R3", "R4"}

HOUSING_TYPES = [
    "dual_occupancy",
    "multi_dwelling_housing",
    "multi_dwelling_housing_terraces",
    "residential_flat_building",
]

DIMENSIONAL_PARAMS = {"min_lot_size", "min_lot_width"}

# Walking factor: straight-line × factor = approximate walking distance.
# 1.35 is appropriate for inner-Sydney suburbs with street grids and some
# water / detour constraints (Iron Cove, Parramatta River edges).
WALKING_FACTOR = 1.35

# LMR thresholds in metres (walking distance)
INNER_THRESHOLD = 400
OUTER_THRESHOLD = 800

# NSW ArcGIS endpoint for Town Centres Map (SEPP_Housing_2021 Layer 6)
NSW_TC_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
    "/Planning/SEPP_Housing_2021/MapServer/6/query"
)


# ----------------------------------------------------------------------------
# Town Centres — load from cache or fetch live
# ----------------------------------------------------------------------------

_TC_SHAPES: list[tuple[str, Polygon]] | None = None   # module-level cache


def _load_tc_shapes() -> list[tuple[str, Polygon]]:
    """
    Return list of (label, Shapely Polygon) for all town centre polygons.
    Reads from data/town_centres.geojson (fast path).
    Falls back to live NSW API query and saves the result.
    """
    global _TC_SHAPES
    if _TC_SHAPES is not None:
        return _TC_SHAPES

    cache_path = DATA_DIR / "town_centres.geojson"
    if cache_path.exists():
        with open(cache_path) as f:
            fc = json.load(f)
        shapes = []
        for feat in fc["features"]:
            label = feat["properties"].get("LABEL", "unknown")
            rings = feat["geometry"]["coordinates"]
            try:
                poly = Polygon([(p[0], p[1]) for p in rings[0]])
                if poly.is_valid:
                    shapes.append((label, poly))
            except Exception:
                pass
        _TC_SHAPES = shapes
        print(f"  Town centres: loaded {len(shapes)} polygons from cache")
        return _TC_SHAPES

    # No cache — fetch from NSW API
    print("  Town centres: cache not found, fetching from NSW API...")
    _TC_SHAPES = fetch_town_centres(save=True)
    return _TC_SHAPES


def fetch_town_centres(
    bbox: str = "150.90,-34.00,151.30,-33.75",
    save: bool = True,
) -> list[tuple[str, Polygon]]:
    """
    Fetch town centre polygons from NSW SEPP_Housing_2021 Layer 6.
    Saves result to data/town_centres.geojson if save=True.
    Returns list of (label, Shapely Polygon).
    """
    params = {
        "geometry": bbox,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,LABEL,LAY_CLASS,LGA_NAME",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    resp = requests.get(NSW_TC_URL, params=params, timeout=20)
    resp.raise_for_status()
    features = resp.json().get("features", [])

    shapes = []
    fc_features = []
    for feat in features:
        label = feat["attributes"].get("LABEL", "unknown")
        rings = feat["geometry"]["rings"]
        try:
            poly = Polygon([(p[0], p[1]) for p in rings[0]])
            if poly.is_valid:
                shapes.append((label, poly))
                fc_features.append({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": rings},
                    "properties": feat["attributes"],
                })
        except Exception:
            pass

    if save:
        fc = {
            "type": "FeatureCollection",
            "source": "NSW SEPP_Housing_2021 MapServer Layer 6 (Town Centres Map)",
            "features": fc_features,
        }
        with open(DATA_DIR / "town_centres.geojson", "w") as f:
            json.dump(fc, f)
        print(f"  Saved {len(shapes)} town centre polygons to data/town_centres.geojson")

    return shapes


# ----------------------------------------------------------------------------
# LMR spatial check
# ----------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two WGS84 points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_lmr_status(lat: float, lon: float) -> dict:
    """
    Determine if a point is in an LMR inner area, outer area, or outside.

    Queries cached town centre polygons. For each polygon, finds the nearest
    point on the polygon boundary to (lat, lon), converts to walking distance,
    and applies the 400m / 800m thresholds.

    Returns:
        {
            "status":       "inner" | "outer" | None,
            "walking_m":    float,          # to nearest TC boundary point
            "nearest_tc":   str,            # town centre name
            "straight_m":   float,
        }
    """
    tc_shapes = _load_tc_shapes()
    if not tc_shapes:
        return {"status": None, "walking_m": None, "nearest_tc": "no data", "straight_m": None}

    lot_pt = Point(lon, lat)
    best_walk = float("inf")
    best_sl   = float("inf")
    best_tc   = ""

    for label, tc_poly in tc_shapes:
        np_on_tc, _ = nearest_points(tc_poly.boundary, lot_pt)
        sl   = haversine(lat, lon, np_on_tc.y, np_on_tc.x)
        walk = sl * WALKING_FACTOR
        if walk < best_walk:
            best_walk = walk
            best_sl   = sl
            best_tc   = label

    if best_walk < INNER_THRESHOLD:
        status = "inner"
    elif best_walk < OUTER_THRESHOLD:
        status = "outer"
    else:
        status = None

    return {
        "status":     status,
        "walking_m":  round(best_walk, 1),
        "straight_m": round(best_sl, 1),
        "nearest_tc": best_tc,
    }


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------

def compute_lot_dimensions(
    polygon_coords: list, geocoded_lat: float, geocoded_lon: float
) -> dict:
    """
    Compute lot area and front-edge width from a WGS84 polygon ring.

    polygon_coords: list of [lon, lat] pairs (closing point included)
    geocoded_lat/lon: Nominatim road point — identifies the front edge.
    """
    lot = Polygon(polygon_coords)
    # 1 degree of longitude is ~111,320m x cos(latitude), not 111,000m —
    # treating both axes as equal overstates area by ~1/cos(lat) (~20% at
    # this latitude). Matches the correction in compute_envelope._metric_scale.
    lat_rad_for_area = math.radians(geocoded_lat)
    lot_area_sqm = lot.area * 111_320 * math.cos(lat_rad_for_area) * 111_000

    coords = polygon_coords[:-1]
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

def get_applicable_rules(
    sepp_rules: list, housing_type: str, zone: str, lmr_status: str
) -> list:
    """
    Filter SEPP Ch6 rules for a given housing type, zone, and LMR band.

    Includes:
      - lmr_zone == "all_lmr"                (applies to any LMR lot)
      - lmr_zone == "lmr_inner_area"         (only for inner lots)
      - lmr_zone == "lmr_outer_area"         (only for outer lots)
    """
    allowed_zones = {"all_lmr"}
    if lmr_status == "inner":
        allowed_zones.add("lmr_inner_area")
    elif lmr_status == "outer":
        allowed_zones.add("lmr_outer_area")

    return [
        r for r in sepp_rules
        if r.get("housing_type") == housing_type
        and zone in r.get("applicable_zones", [])
        and r.get("lmr_zone") in allowed_zones
    ]


def check_rule(rule: dict, lot_area_sqm: float, front_width_m: float) -> dict:
    """Apply one SEPP Ch6 rule against lot dimensions."""
    param  = rule["parameter"]
    value  = rule["value"]
    unit   = rule["unit"]
    clause = rule.get("source_clause", "")
    op     = rule.get("operator", "?")

    if param == "min_lot_size":
        passed = lot_area_sqm >= value
        return {
            "rule_id":   rule["rule_id"],
            "parameter": param,
            "required":  f">= {value} {unit}",
            "actual":    f"{lot_area_sqm:.1f} {unit}",
            "passed":    passed,
            "clause":    clause,
            "type":      "dimensional",
        }

    if param == "min_lot_width":
        passed = front_width_m >= value
        return {
            "rule_id":   rule["rule_id"],
            "parameter": param,
            "required":  f">= {value} {unit}",
            "actual":    f"{front_width_m:.1f} {unit}",
            "passed":    passed,
            "clause":    clause,
            "note":      "measured at front boundary (proxy for front building line)",
            "type":      "dimensional",
        }

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


# ----------------------------------------------------------------------------
# Main eligibility check
# ----------------------------------------------------------------------------

def check_lmr_eligibility(address: str, lot_data: dict, sepp_rules: list) -> dict:
    """
    Full LMR pre-flight check for one address.

    1. Zone check (R1/R2/R3/R4 required by SEPP Ch6 s161)
    2. Spatial LMR check (nearest-boundary distance to NSW Town Centres Map)
    3. Compute lot dimensions from cached polygon
    4. For each housing type, apply applicable SEPP rules (all_lmr + band-specific)
    5. Eligible = all dimensional checks pass
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

    # Spatial LMR status from actual NSW Town Centres Map
    lmr = get_lmr_status(lot_data["lat"], lot_data["lon"])
    lmr_status  = lmr["status"]          # "inner" | "outer" | None
    walking_m   = lmr["walking_m"]
    straight_m  = lmr["straight_m"]
    nearest_tc  = lmr["nearest_tc"]

    if lmr_status is None:
        return {
            "address":           address,
            "zone":              zone,
            "lmr_zone_eligible": False,
            "reason":            (
                f"Not within 800m walking distance of any NSW Town Centre "
                f"(nearest: {nearest_tc}, {walking_m:.0f}m walking / {straight_m:.0f}m straight-line)"
            ),
            "lmr_spatial": lmr,
            "housing_types": {},
        }

    # Lot dimensions
    polygon_coords = lot_data["polygon"]["coordinates"][0]
    dims = compute_lot_dimensions(polygon_coords, lot_data["lat"], lot_data["lon"])
    lot_area    = dims["lot_area_sqm"]
    front_width = dims["front_width_m"]

    # Per-housing-type checks
    housing_results = {}
    for htype in HOUSING_TYPES:
        applicable = get_applicable_rules(sepp_rules, htype, zone, lmr_status)
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
        "lmr_status":        lmr_status,
        "lmr_spatial":       lmr,
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

    lmr    = result["lmr_spatial"]
    band   = result["lmr_status"].upper()
    print(f"  Zone         : {result['zone']}")
    print(f"  LMR status   : {band} area")
    print(f"  Nearest TC   : {lmr['nearest_tc']}")
    print(f"  Distance     : {lmr['walking_m']:.0f}m walking  ({lmr['straight_m']:.0f}m straight-line × {WALKING_FACTOR})")
    print(f"  Lot area     : {result['lot_area_sqm']:.1f} m2")
    print(f"  Front width  : {result['front_width_m']:.1f} m")

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
            flag = "[PASS]" if c["passed"] is True else ("[FAIL]" if c["passed"] is False else "[INFO]")
            note = f"  <- {c['note']}" if "note" in c else ""
            print(f"  {flag} {c['parameter']:<30} required: {c['required']:<18} actual: {c['actual']}{note}")
            print(f"         {c['clause']}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> list:
    cache_path = DATA_DIR / "cached_lots.json"
    with open(cache_path) as f:
        raw_cache = json.load(f)
    cached = {k.strip(): v for k, v in raw_cache.items()}

    rules_path = DATA_DIR / "rules_housing_sepp_ch6.json"
    with open(rules_path) as f:
        sepp_rules = json.load(f)

    test_addresses = [
        "35 Connecticut Avenue Five Dock 2046",
        "28 Clare Crescent Russell Lea 2046",
        "18 Spring Street Abbotsford 2046",
    ]

    print("\nLMR Pre-flight Validation -- Housing SEPP 2021 Chapter 6")
    print("Town Centres: data/town_centres.geojson (NSW SEPP_Housing_2021 Layer 6)")
    print("Rules       : data/rules_housing_sepp_ch6.json (manually extracted)")
    print(f"Walking factor: {WALKING_FACTOR}  |  Inner < {INNER_THRESHOLD}m  |  Outer < {OUTER_THRESHOLD}m")

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
