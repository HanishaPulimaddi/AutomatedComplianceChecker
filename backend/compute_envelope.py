import json
import math
from shapely.geometry import Polygon, Point, LineString, mapping
from shapely.ops import nearest_points


def get_front_edge(polygon_coords: list, geocoded_lon: float, geocoded_lat: float) -> tuple:
    """
    Identify the front edge of the lot (the one facing the street).

    Uses nearest point on the polygon boundary to the geocoded road point.
    This is more robust than closest-midpoint for crescent roads and corner
    lots where the geocoded point sits at a bend rather than directly in front.
    """
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


def _apply_directional_setbacks(
    lot: Polygon,
    coords: list,
    front_edge_idx: int,
    front_setback_deg: float,
    rear_setback_deg: float,
    side_setback_deg: float,
) -> Polygon:
    """
    Clip the lot polygon by offsetting each edge inward by the correct setback.

    Algorithm: for each edge, build a half-plane (large rectangle on the inward
    side of the offset edge) and intersect the current result with it.
    This correctly separates front, rear, and side setbacks instead of averaging.

    Args:
        lot:               Shapely Polygon of the lot.
        coords:            List of [lon, lat] ring points (closing point included).
        front_edge_idx:    Index of the front edge start vertex (from get_front_edge).
        *_setback_deg:     Setback distances already converted to decimal degrees.

    Returns:
        Shapely Polygon of the buildable area (may be MultiPolygon for odd lots).
    """
    ring = coords[:-1]  # drop closing duplicate
    n = len(ring)
    if n < 3:
        return lot

    # ── Identify rear edge (midpoint furthest from front edge midpoint) ──────
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

    # ── Clip lot edge by edge ────────────────────────────────────────────────
    result = lot
    centroid = lot.centroid

    for i in range(n):
        p1 = ring[i]
        p2 = ring[(i + 1) % n]

        # Choose setback for this edge
        if i == front_edge_idx:
            sb = front_setback_deg
        elif i == rear_edge_idx:
            sb = rear_setback_deg
        else:
            sb = side_setback_deg

        # Edge direction vector
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.sqrt(dx ** 2 + dy ** 2)
        if length < 1e-12:
            continue  # degenerate edge — skip

        # Unit vectors: along edge (ux, uy) and perpendicular (nx, ny)
        ux, uy = dx / length, dy / length
        nx, ny = uy, -ux  # 90° CCW rotation

        # Ensure normal points inward (toward centroid)
        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        if nx * (centroid.x - mx) + ny * (centroid.y - my) < 0:
            nx, ny = -nx, -ny

        # Offset edge inward by setback
        op1 = (p1[0] + nx * sb, p1[1] + ny * sb)
        op2 = (p2[0] + nx * sb, p2[1] + ny * sb)

        # Build half-plane: big rectangle on the inward side of the offset edge.
        # Extend 2° in every direction so it always covers the entire lot.
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
    front_edge_idx, _ = get_front_edge(
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

    # Apply directional setbacks: clip the lot with a half-plane per edge.
    # Each edge is offset inward by its specific setback (front / rear / side).
    # This replaces the previous uniform average buffer which was inaccurate
    # by up to 50% for lots where front >> side (4.5m vs 0.9m in Canada Bay R2).
    # front_edge_idx already computed above by get_front_edge.
    envelope = _apply_directional_setbacks(
        lot, coords, front_edge_idx,
        front_setback, rear_setback, side_setback
    )

    if envelope is None or envelope.is_empty:
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