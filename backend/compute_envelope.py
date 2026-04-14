import json
import math
from shapely.geometry import Polygon, mapping


def get_front_edge(polygon_coords: list, geocoded_lon: float, geocoded_lat: float) -> tuple:
    """
    Identify the front edge of the lot (the one facing the street).
    The edge whose midpoint is closest to the geocoded point (which lands on the road)
    is the front edge.
    """
    coords = polygon_coords[:-1]  # remove closing point
    closest_idx = 0
    closest_dist = float("inf")

    for i in range(len(coords)):
        p1 = coords[i]
        p2 = coords[(i + 1) % len(coords)]
        

        mid_x = (p1[0] + p2[0]) / 2
        mid_y = (p1[1] + p2[1]) / 2

        dist = math.sqrt((mid_x - geocoded_lon)**2 + (mid_y - geocoded_lat)**2)

        if dist < closest_dist:
            closest_dist = dist
            closest_idx = i

    p1 = coords[closest_idx]
    p2 = coords[(closest_idx + 1) % len(coords)]
    edge_length = math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2) * 111000
    mid_x = (p1[0] + p2[0]) / 2
    mid_y = (p1[1] + p2[1]) / 2

    print(f"  Front edge: index {closest_idx}, "
          f"length {edge_length:.1f}m, "
          f"dist to road {closest_dist*111000:.1f}m")

    return closest_idx, (mid_x, mid_y)


def compute_envelope(lot_polygon: dict, rules: list,
                     geocoded_lat: float, geocoded_lon: float) -> dict:
    """
    Given a lot polygon and a list of rules, compute the buildable envelope.

    lot_polygon:   GeoJSON Polygon {"type": "Polygon", "coordinates": [...]}
    rules:         list of rule dicts from rules_r2_canada_bay_raw.json
    geocoded_lat:  raw geocoded latitude (lands on the road)
    geocoded_lon:  raw geocoded longitude (lands on the road)

    Returns a GeoJSON Polygon of the buildable area.
    """
    coords = lot_polygon["coordinates"][0]
    lot = Polygon(coords)

    lot_area_sqm = lot.area * (111000 ** 2)
    print(f"  Lot area: {lot_area_sqm:.1f} sqm")

    # Identify front edge using geocoded point
    front_edge_idx, front_midpoint = get_front_edge(
        coords, geocoded_lon, geocoded_lat
    )

    # Convert metres to degrees (1 degree lat ≈ 111,000m at Sydney's latitude)
    M_TO_DEG = 1 / 111000

    # Extract setback values from real rules
    front_setback      = None
    rear_setback       = None
    side_setback       = None
    side_setback_upper = None

    for rule in rules:
        param = rule.get("parameter")
        op    = rule.get("operator")

        if param == "front_setback" and op == "min":
            if front_setback is None:
                front_setback = rule["value"] * M_TO_DEG
                print(f"  Front setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "rear_setback" and op == "min":
            if rear_setback is None:
                rear_setback = rule["value"] * M_TO_DEG
                print(f"  Rear setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_ground" and op == "min":
            if side_setback is None:
                side_setback = rule["value"] * M_TO_DEG
                print(f"  Side setback (ground) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_upper" and op == "min":
            if side_setback_upper is None:
                side_setback_upper = rule["value"] * M_TO_DEG
                print(f"  Side setback (upper) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

    # Fall back to Canada Bay DCP R2 defaults only if rule not found
    if front_setback is None:
        print("  WARNING: no front_setback rule found, using default 4.5m")
        front_setback = 4.5 * M_TO_DEG
    if rear_setback is None:
        print("  WARNING: no rear_setback rule found, using default 4.0m")
        rear_setback = 4.0 * M_TO_DEG
    if side_setback is None:
        print("  WARNING: no side_setback_ground rule found, using default 0.9m")
        side_setback = 0.9 * M_TO_DEG
    if side_setback_upper is None:
        print("  WARNING: no side_setback_upper rule found, using default 1.5m")
        side_setback_upper = 1.5 * M_TO_DEG

    print(f"  Setbacks — front: {front_setback*111000:.1f}m, "
          f"rear: {rear_setback*111000:.1f}m, "
          f"side (ground): {side_setback*111000:.1f}m, "
          f"side (upper): {side_setback_upper*111000:.1f}m")

    # Apply uniform inward buffer using ground floor side setback
    # Week 2 will apply directional setbacks per edge
    avg_setback = (front_setback + rear_setback + side_setback) / 3
    envelope = lot.buffer(-avg_setback)

    if envelope.is_empty:
        raise ValueError("Lot is too small for the required setbacks!")

    envelope_area_sqm = envelope.area * (111000 ** 2)
    print(f"  Buildable envelope area: {envelope_area_sqm:.1f} sqm")
    print(f"  Coverage: {(envelope_area_sqm/lot_area_sqm)*100:.1f}% of lot")

    return mapping(envelope)


if __name__ == "__main__":
    # Load cached lots
    with open("data/cached_lots.json") as f:
        cached = json.load(f)

    # Load real extracted rules
    with open("data/rules_r2_canada_bay.json") as f:
        all_rules = json.load(f)

    # Only use high confidence rules
    rules = [r for r in all_rules if r.get("confidence", 0) >= 0.8]
    print(f"Loaded {len(rules)} rules with confidence >= 0.8 "
          f"(out of {len(all_rules)} total)\n")

    results = {}

    for address, lot_data in cached.items():
        print(f"Computing envelope for: {address}")
        polygon = lot_data["polygon"]
        lat     = lot_data["lat"]
        lon     = lot_data["lon"]

        envelope = compute_envelope(polygon, rules, lat, lon)

        results[address] = {
            "lot_polygon":   polygon,
            "envelope":      envelope,
            "rules_applied": rules
        }
        print()

    with open("data/envelopes.json", "w") as f:
        json.dump(results, f, indent=2)

    print("Done! Saved to data/envelopes.json")