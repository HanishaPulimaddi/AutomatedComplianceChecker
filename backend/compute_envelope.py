"""
compute_envelope.py — Buildable envelope calculator for NSW residential lots.

Computes the maximum buildable footprint for a lot by applying directional
setbacks (front, rear, side) extracted from council DCP rules. The lot polygon
is clipped edge-by-edge using half-plane intersections, producing a smaller
polygon that respects all required boundary offsets.

The front edge is identified by proximity to the geocoded road point (from
Nominatim), and the rear edge is the one whose midpoint is furthest from
the front. All other edges are treated as side boundaries.

When a setback rule is missing from the extracted rules, the system falls
back to LGA-specific defaults rather than hardcoding Canada Bay values.
Each setback in the output is tagged with its source ("rule" or "default")
so downstream consumers can distinguish extracted rules from fallbacks.

Lots that are too small for the required setbacks are handled gracefully —
an empty envelope is returned and the lot is skipped in batch mode rather
than crashing the entire run.

Also computes non-spatial development controls (FSR, height, parking,
landscaping, lot requirements, fencing, building separation, access) and 
bundles them alongside the envelope geometry for the API response.
"""

import json
import math
from shapely.geometry import Polygon, Point, LineString, mapping, shape
from shapely.ops import nearest_points


# ----------------------------------------------------------------------------
# LGA-specific default setbacks (metres) used when extracted rules are missing.
# These are conservative minimums from each council's DCP for R2 zones.
# Add new LGAs here as coverage expands.
# ----------------------------------------------------------------------------

LGA_DEFAULT_SETBACKS = {
    "Canada Bay": {
        "front_setback": 4.5,
        "rear_setback": 4.0,
        "side_setback_ground": 0.9,
        "side_setback_upper": 1.5,
    },
    "Inner West": {
        "front_setback": 4.0,
        "rear_setback": 3.0,
        "side_setback_ground": 0.9,
        "side_setback_upper": 1.5,
    },
    "Burwood": {
        "front_setback": 5.5,
        "rear_setback": 6.0,
        "side_setback_ground": 0.9,
        "side_setback_upper": 1.5,
    },
}

# Used when the LGA is unknown or not in the table above.
FALLBACK_DEFAULTS = {
    "front_setback": 4.5,
    "rear_setback": 4.0,
    "side_setback_ground": 0.9,
    "side_setback_upper": 1.5,
}


# Identify the front edge of the lot by finding the polygon edge closest to the geocoded road point.
def get_front_edge(polygon_coords: list, geocoded_lon: float, geocoded_lat: float) -> tuple:
    coords = polygon_coords[:-1]  # remove closing point
    lot = Polygon(polygon_coords)
    road_pt = Point(geocoded_lon, geocoded_lat)

    nearest_on_boundary, _ = nearest_points(lot.boundary, road_pt)

    front_idx = 0
    for i in range(len(coords)):
        p1 = coords[i]
        p2 = coords[(i + 1) % len(coords)]
        if LineString([p1, p2]).distance(nearest_on_boundary) < 1e-8:
            front_idx = i
            break

    p1 = coords[front_idx]
    p2 = coords[(front_idx + 1) % len(coords)]

    lat_rad = math.radians(geocoded_lat)
    dx_m = (p2[0] - p1[0]) * 111000 * math.cos(lat_rad)
    dy_m = (p2[1] - p1[1]) * 111000
    edge_length = math.sqrt(dx_m**2 + dy_m**2)

    dist_m = road_pt.distance(nearest_on_boundary) * 111000
    mid_x = (p1[0] + p2[0]) / 2
    mid_y = (p1[1] + p2[1]) / 2

    print(f"  Front edge: index {front_idx}, "
          f"length {edge_length:.1f}m, "
          f"dist to road {dist_m:.1f}m")

    return front_idx, (mid_x, mid_y)


# Clip the lot polygon by offsetting each edge inward by the correct setback (front/rear/side).
def _apply_directional_setbacks(
    lot: Polygon,
    coords: list,
    front_edge_idx: int,
    front_setback_deg: float,
    rear_setback_deg: float,
    side_setback_deg: float,
) -> Polygon:
    ring = coords[:-1]  # drop closing duplicate
    n = len(ring)
    if n < 3:
        return lot

    # Identify rear edge (midpoint furthest from front edge midpoint)
    fp1 = ring[front_edge_idx]
    fp2 = ring[(front_edge_idx + 1) % n]
    front_mid_x = (fp1[0] + fp2[0]) / 2
    front_mid_y = (fp1[1] + fp2[1]) / 2

    rear_edge_idx = 0
    max_dist = -1.0
    for i in range(n):
        if i == front_edge_idx:
            continue
        p1 = ring[i]
        p2 = ring[(i + 1) % n]
        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        d = (mx - front_mid_x) ** 2 + (my - front_mid_y) ** 2
        if d > max_dist:
            max_dist = d
            rear_edge_idx = i

    # Clip lot edge by edge using half-plane intersection
    result = lot
    centroid = lot.centroid

    for i in range(n):
        p1 = ring[i]
        p2 = ring[(i + 1) % n]

        if i == front_edge_idx:
            sb = front_setback_deg
        elif i == rear_edge_idx:
            sb = rear_setback_deg
        else:
            sb = side_setback_deg

        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.sqrt(dx ** 2 + dy ** 2)
        if length < 1e-12:
            continue

        ux, uy = dx / length, dy / length
        nx, ny = uy, -ux

        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        if nx * (centroid.x - mx) + ny * (centroid.y - my) < 0:
            nx, ny = -nx, -ny

        op1 = (p1[0] + nx * sb, p1[1] + ny * sb)
        op2 = (p2[0] + nx * sb, p2[1] + ny * sb)

        BIG = 2.0
        half_plane = Polygon([
            (op1[0] - ux * BIG,           op1[1] - uy * BIG),
            (op2[0] + ux * BIG,           op2[1] + uy * BIG),
            (op2[0] + ux * BIG + nx * BIG, op2[1] + uy * BIG + ny * BIG),
            (op1[0] - ux * BIG + nx * BIG, op1[1] - uy * BIG + ny * BIG),
        ])

        result = result.intersection(half_plane)
        if result.is_empty:
            break

    return result


CONTROL_PARAMETERS = {
    # Density
    "fsr",
    "site_coverage_pct",
    
    # Height & bulk
    "max_height",
    "max_storeys",
    "height_plane",
    "max_wall_height",
    
    # Setbacks (handled separately in envelope computation, but included for completeness)
    "front_setback",
    "rear_setback",
    "rear_setback_upper",
    "side_setback_ground",
    "side_setback_upper",
    "basement_setback",
    
    # Lot requirements
    "min_lot_size",
    "min_lot_width",
    "min_dwelling_width",
    
    # Landscaping & open space
    "landscaped_area_pct",
    "landscaped_area_front_pct",
    "landscaped_area_rear_pct",
    "private_open_space",
    "private_open_space_min_dimension",
    
    # Parking & access
    "min_parking_spaces",
    "max_parking_spaces",
    "min_parking_per_dwelling",
    "parking_spaces_per_dwelling",
    "car_space_length",
    "car_space_width",
    "min_parking_space_length",
    "min_parking_space_width",
    "min_bicycle_spaces",
    "max_driveway_width",
    "driveway_setback",
    
    # Building separation & setbacks
    "building_separation",
    "balcony_rear_setback",
    "balcony_side_setback",
    "outbuilding_setback",
    
    # Fencing
    "front_fence_height_solid",
    "front_fence_height_open",
    "side_fence_height",
    "rear_fence_height",
}


# Convert a Shapely geometry's area from square degrees to square metres.
def _area_sqm(geometry) -> float:
    return round(geometry.area * (111000 ** 2), 1)


# Extract display-ready fields from a single rule dict for the API response.
def _control_summary(rule: dict | None) -> dict | None:
    if not rule:
        return None
    return {
        "value": rule.get("value"),
        "unit": rule.get("unit"),
        "operator": rule.get("operator"),
        "clause": rule.get("source_clause", ""),
        "source_document": rule.get("source_document", ""),
        "source_type": rule.get("source_type", ""),
        "confidence": rule.get("confidence"),
        "verified": rule.get("verified", False),
    }


# Return the first rule matching a given parameter name, or None.
def _first_rule_by_parameter(rules: list, parameter: str) -> dict | None:
    for rule in rules:
        if rule.get("parameter") == parameter:
            return rule
    return None


# Build a dict of control summaries for a list of parameter names.
def _control_group(rules: list, parameters: list[str]) -> dict:
    return {
        param: _control_summary(_first_rule_by_parameter(rules, param))
        for param in parameters
    }


# Compute non-spatial development controls (FSR, height, parking, landscaping, lot requirements, fencing, access).
def compute_development_controls(
    lot_polygon: dict,
    envelope_polygon: dict,
    rules: list,
) -> dict:
    lot = Polygon(lot_polygon["coordinates"][0])
    envelope = shape(envelope_polygon)
    lot_area_sqm = _area_sqm(lot)
    envelope_area_sqm = _area_sqm(envelope)
    envelope_coverage_pct = (
        round((envelope_area_sqm / lot_area_sqm) * 100, 1)
        if lot_area_sqm
        else None
    )

    fsr_rule = _first_rule_by_parameter(rules, "fsr")
    fsr_value = fsr_rule.get("value") if fsr_rule else None
    max_floor_area_sqm = (
        round(lot_area_sqm * float(fsr_value), 1)
        if fsr_value is not None
        else None
    )

    present_params = {rule.get("parameter") for rule in rules}

    return {
        "lot_area_sqm": lot_area_sqm,
        "buildable_envelope_area_sqm": envelope_area_sqm,
        "buildable_envelope_coverage_pct": envelope_coverage_pct,
        "density": {
            "fsr": _control_summary(fsr_rule),
            "max_floor_area_sqm": max_floor_area_sqm,
            "site_coverage_pct": _control_summary(
                _first_rule_by_parameter(rules, "site_coverage_pct")
            ),
        },
        "height": _control_group(
            rules,
            ["max_height", "max_storeys", "height_plane", "max_wall_height"],
        ),
        "parking": _control_group(
            rules,
            [
                "min_parking_spaces",
                "max_parking_spaces",
                "min_parking_per_dwelling",
                "parking_spaces_per_dwelling",
                "car_space_length",
                "car_space_width",
                "min_parking_space_length",
                "min_parking_space_width",
                "min_bicycle_spaces",
            ],
        ),
        "access": _control_group(
            rules,
            [
                "max_driveway_width",
                "driveway_setback",
            ],
        ),
        "landscaping_open_space": _control_group(
            rules,
            [
                "landscaped_area_pct",
                "landscaped_area_front_pct",
                "landscaped_area_rear_pct",
                "private_open_space",
                "private_open_space_min_dimension",
            ],
        ),
        "building_separation": _control_group(
            rules,
            [
                "building_separation",
                "balcony_rear_setback",
                "balcony_side_setback",
            ],
        ),
        "lot_requirements": _control_group(
            rules,
            ["min_lot_size", "min_lot_width", "min_dwelling_width"],
        ),
        "fencing": _control_group(
            rules,
            [
                "front_fence_height_solid",
                "front_fence_height_open",
                "side_fence_height",
                "rear_fence_height",
            ],
        ),
        "setbacks": _control_group(
            rules,
            [
                "front_setback",
                "rear_setback",
                "rear_setback_upper",
                "side_setback_ground",
                "side_setback_upper",
                "basement_setback",
                "outbuilding_setback",
            ],
        ),
        "missing_parameters": sorted(CONTROL_PARAMETERS - present_params),
    }


# Wrapper that computes the envelope polygon and development controls together.
def compute_envelope_result(
    lot_polygon: dict,
    rules: list,
    geocoded_lat: float,
    geocoded_lon: float,
    lga: str = "",
) -> dict:
    envelope, setback_sources = compute_envelope(
        lot_polygon, rules, geocoded_lat, geocoded_lon, lga=lga
    )

    # Check if envelope is empty (lot too small for setbacks)
    envelope_geom = shape(envelope) if envelope.get("coordinates") else None
    if envelope_geom is None or envelope_geom.is_empty or envelope_geom.area == 0:
        return {
            "envelope": envelope,
            "setback_sources": setback_sources,
            "development_controls": None,
            "skipped": True,
            "skip_reason": "Lot is too small for the required setbacks",
        }

    return {
        "envelope": envelope,
        "setback_sources": setback_sources,
        "development_controls": compute_development_controls(
            lot_polygon,
            envelope,
            rules,
        ),
        "skipped": False,
    }


# Look up the correct default setback for a parameter given the lot's LGA.
def _get_default_setback(parameter: str, lga: str) -> float:
    lga_lower = lga.strip().lower()
    for lga_name, defaults in LGA_DEFAULT_SETBACKS.items():
        if lga_name.lower() in lga_lower or lga_lower in lga_name.lower():
            value = defaults.get(parameter)
            if value is not None:
                return value
    return FALLBACK_DEFAULTS[parameter]


# Main orchestrator: compute the buildable envelope polygon from lot geometry and DCP rules.
def compute_envelope(
    lot_polygon: dict,
    rules: list,
    geocoded_lat: float,
    geocoded_lon: float,
    lga: str = "",
) -> tuple[dict, dict]:
    """
    Returns:
        (envelope_geojson, setback_sources)
        setback_sources is a dict like:
            {"front_setback": {"value_m": 1.5, "source": "rule", "clause": "E2.1 C3"},
             "rear_setback":  {"value_m": 4.0, "source": "default", "lga": "Inner West"}, ...}
    """
    coords = lot_polygon["coordinates"][0]
    lot = Polygon(coords)

    lot_area_sqm = lot.area * (111000 ** 2)
    print(f"  Lot area: {lot_area_sqm:.1f} sqm")

    front_edge_idx, _ = get_front_edge(
        coords, geocoded_lon, geocoded_lat
    )

    M_TO_DEG = 1 / 111000

    # Extract setback values from rules, tracking source for each
    front_setback = None
    rear_setback = None
    side_setback = None
    side_setback_upper = None

    setback_sources = {}

    for rule in rules:
        param = rule.get("parameter")
        op = rule.get("operator")

        if param == "front_setback" and op == "min":
            if front_setback is None:
                front_setback = rule["value"] * M_TO_DEG
                setback_sources["front_setback"] = {
                    "value_m": rule["value"],
                    "source": "rule",
                    "clause": rule.get("source_clause", ""),
                }
                print(f"  Front setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "rear_setback" and op == "min":
            if rear_setback is None:
                rear_setback = rule["value"] * M_TO_DEG
                setback_sources["rear_setback"] = {
                    "value_m": rule["value"],
                    "source": "rule",
                    "clause": rule.get("source_clause", ""),
                }
                print(f"  Rear setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_ground" and op == "min":
            if side_setback is None:
                side_setback = rule["value"] * M_TO_DEG
                setback_sources["side_setback_ground"] = {
                    "value_m": rule["value"],
                    "source": "rule",
                    "clause": rule.get("source_clause", ""),
                }
                print(f"  Side setback (ground) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_upper" and op == "min":
            if side_setback_upper is None:
                side_setback_upper = rule["value"] * M_TO_DEG
                setback_sources["side_setback_upper"] = {
                    "value_m": rule["value"],
                    "source": "rule",
                    "clause": rule.get("source_clause", ""),
                }
                print(f"  Side setback (upper) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

    # Fall back to LGA-specific defaults for any missing setback
    lga_label = lga or "unknown"

    if front_setback is None:
        default_val = _get_default_setback("front_setback", lga)
        front_setback = default_val * M_TO_DEG
        setback_sources["front_setback"] = {
            "value_m": default_val,
            "source": "default",
            "lga": lga_label,
        }
        print(f"  WARNING: no front_setback rule found, using {lga_label} default {default_val}m")

    if rear_setback is None:
        default_val = _get_default_setback("rear_setback", lga)
        rear_setback = default_val * M_TO_DEG
        setback_sources["rear_setback"] = {
            "value_m": default_val,
            "source": "default",
            "lga": lga_label,
        }
        print(f"  WARNING: no rear_setback rule found, using {lga_label} default {default_val}m")

    if side_setback is None:
        default_val = _get_default_setback("side_setback_ground", lga)
        side_setback = default_val * M_TO_DEG
        setback_sources["side_setback_ground"] = {
            "value_m": default_val,
            "source": "default",
            "lga": lga_label,
        }
        print(f"  WARNING: no side_setback_ground rule found, using {lga_label} default {default_val}m")

    if side_setback_upper is None:
        default_val = _get_default_setback("side_setback_upper", lga)
        side_setback_upper = default_val * M_TO_DEG
        setback_sources["side_setback_upper"] = {
            "value_m": default_val,
            "source": "default",
            "lga": lga_label,
        }
        print(f"  WARNING: no side_setback_upper rule found, using {lga_label} default {default_val}m")

    print(f"  Setbacks — front: {front_setback*111000:.1f}m, "
          f"rear: {rear_setback*111000:.1f}m, "
          f"side (ground): {side_setback*111000:.1f}m, "
          f"side (upper): {side_setback_upper*111000:.1f}m")

    envelope = _apply_directional_setbacks(
        lot, coords, front_edge_idx,
        front_setback, rear_setback, side_setback
    )

    if envelope is None or envelope.is_empty:
        print(f"  WARNING: Lot is too small for the required setbacks — returning empty envelope")
        empty_polygon = {"type": "Polygon", "coordinates": []}
        return empty_polygon, setback_sources

    envelope_area_sqm = envelope.area * (111000 ** 2)
    print(f"  Buildable envelope area: {envelope_area_sqm:.1f} sqm")
    print(f"  Coverage: {(envelope_area_sqm/lot_area_sqm)*100:.1f}% of lot")

    return mapping(envelope), setback_sources


if __name__ == "__main__":
    # Load cached lots
    with open("data/cached_lots.json", encoding="utf-8") as f:
        cached = json.load(f)

    # Load ALL rule files (both LGAs, both LEP and DCP)
    print("Loading rules...")
    
    # Inner West LEP (has FSR!)
    with open("data/rules_inner_west_lep.json", encoding="utf-8") as f:
        iw_lep = json.load(f)
    print(f"  Loaded {len(iw_lep)} Inner West LEP rules")
    
    # Inner West DCP
    with open("data/rules_inner_west_marrickville.json", encoding="utf-8") as f:
        iw_marr = json.load(f)
    print(f"  Loaded {len(iw_marr)} Inner West Marrickville DCP rules")
    
    with open("data/rules_inner_west_ashfield.json", encoding="utf-8") as f:
        iw_ash = json.load(f)
    print(f"  Loaded {len(iw_ash)} Inner West Ashfield DCP rules")
    
    # Canada Bay DCP
    with open("data/rules_r2_canada_bay.json", encoding="utf-8") as f:
        cb_dcp = json.load(f)
    print(f"  Loaded {len(cb_dcp)} Canada Bay DCP rules")

    # Merge: LEP first (priority), then high-confidence DCP
    all_rules = iw_lep + [r for r in (cb_dcp + iw_marr + iw_ash) if r.get("confidence", 0) >= 0.8]
    
    print(f"Total rules after filtering: {len(all_rules)}\n")

    results = {}
    skipped = []

    for address, lot_data in cached.items():
        print(f"Computing envelope for: {address}")
        polygon = lot_data["polygon"]
        lat = lot_data["lat"]
        lon = lot_data["lon"]
        lga = lot_data.get("lga", "")

        envelope_result = compute_envelope_result(polygon, all_rules, lat, lon, lga=lga)

        if envelope_result.get("skipped"):
            print(f"  SKIPPED: {address} — {envelope_result['skip_reason']}")
            skipped.append(address)
            print()
            continue

        results[address] = {
            "lot_polygon": polygon,
            "envelope": envelope_result["envelope"],
            "setback_sources": envelope_result["setback_sources"],
            "development_controls": envelope_result["development_controls"],
            "rules_applied": all_rules,
        }

        # Print development controls summary
        dc = envelope_result["development_controls"]
        if dc:
            density = dc.get("density", {})
            height = dc.get("height", {})
            parking = dc.get("parking", {})
            landscaping = dc.get("landscaping_open_space", {})

            fsr = density.get("fsr")
            max_floor = density.get("max_floor_area_sqm")
            max_h = height.get("max_height")
            max_s = height.get("max_storeys")

            print(f"  FSR            : {fsr['value']} ({fsr['clause']})" if fsr else "  FSR            : not found")
            print(f"  Max floor area : {max_floor} sqm" if max_floor else "  Max floor area : not found")
            print(f"  Max height     : {max_h['value']} {max_h['unit']} ({max_h['clause']})" if max_h else "  Max height     : not found")
            print(f"  Max storeys    : {int(max_s['value'])} ({max_s['clause']})" if max_s else "  Max storeys    : not found")

            min_park = parking.get("min_parking_spaces")
            max_park = parking.get("max_parking_spaces")
            per_dwell = parking.get("min_parking_per_dwelling") or parking.get("parking_spaces_per_dwelling")
            sp_len = parking.get("car_space_length")
            sp_wid = parking.get("car_space_width")

            print(f"  Parking:")
            print(f"    Min spaces       : {min_park['value']} ({min_park['clause']})" if min_park else "    Min spaces       : not found")
            print(f"    Max spaces       : {max_park['value']} ({max_park['clause']})" if max_park else "    Max spaces       : not found")
            print(f"    Per dwelling     : {per_dwell['value']} ({per_dwell['clause']})" if per_dwell else "    Per dwelling     : not found")
            print(f"    Space length     : {sp_len['value']} {sp_len['unit']} ({sp_len['clause']})" if sp_len else "    Space length     : not found")
            print(f"    Space width      : {sp_wid['value']} {sp_wid['unit']} ({sp_wid['clause']})" if sp_wid else "    Space width      : not found")

            # Print landscaping
            landscaped_pct = landscaping.get("landscaped_area_pct")
            landscaped_front = landscaping.get("landscaped_area_front_pct")
            landscaped_rear = landscaping.get("landscaped_area_rear_pct")
            private_os = landscaping.get("private_open_space")
            private_dim = landscaping.get("private_open_space_min_dimension")

            print(f"  Landscaping:")
            print(f"    Total landscaped : {landscaped_pct['value']}{landscaped_pct['unit']} ({landscaped_pct['clause']})" if landscaped_pct else "    Total landscaped : not found")
            print(f"    Front landscaped : {landscaped_front['value']}{landscaped_front['unit']} ({landscaped_front['clause']})" if landscaped_front else "    Front landscaped : not found")
            print(f"    Rear landscaped  : {landscaped_rear['value']}{landscaped_rear['unit']} ({landscaped_rear['clause']})" if landscaped_rear else "    Rear landscaped  : not found")
            print(f"    Private open     : {private_os['value']}{private_os['unit']} ({private_os['clause']})" if private_os else "    Private open     : not found")
            print(f"    Min dimension    : {private_dim['value']}{private_dim['unit']} ({private_dim['clause']})" if private_dim else "    Min dimension    : not found")

        print()

    with open("data/envelopes.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Done! Saved {len(results)} envelopes to data/envelopes.json")

    if skipped:
        print(f"\nSkipped {len(skipped)} address(es) — lot too small for required setbacks:")
        for addr in skipped:
            print(f"  - {addr}")