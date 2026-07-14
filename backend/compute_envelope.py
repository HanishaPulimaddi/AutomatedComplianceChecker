import json
import math
from shapely.geometry import Polygon, Point, LineString, mapping, shape
from shapely.ops import nearest_points, transform

from nsw_apis import get_road_segments_near_point


# Degrees of longitude and latitude are NOT the same physical distance except
# at the equator: 1° latitude is ~111,000m everywhere, but 1° longitude is
# ~111,320m × cos(latitude) — at Sydney's latitude (~-33.85°) that's ~92,400m,
# about 17% less. Every distance/area calculation in this module used to
# multiply raw lon/lat degrees by a flat 111,000 for both axes, which
# systematically under-clipped east/west-facing setbacks and overstated every
# area figure (lot area, envelope area, FSR-derived floor area) by the same
# ~1/cos(lat) factor. These two helpers project to a local metric coordinate
# system (isotropic, centred on the lot's own latitude) so all geometry ops —
# distances, offsets, areas — are done in real metres, then project back.
def _metric_scale(ref_lat: float) -> tuple[float, float]:
    return 111320 * math.cos(math.radians(ref_lat)), 111000


def _project_to_m(coords: list, ref_lat: float) -> list:
    sx, sy = _metric_scale(ref_lat)
    return [(lon * sx, lat * sy) for lon, lat in coords]


def _unproject_geometry(geometry, ref_lat: float):
    sx, sy = _metric_scale(ref_lat)
    return transform(lambda x, y, z=None: (x / sx, y / sy), geometry)


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


def _unit_dir(p1, p2):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-12 else None


def _road_runs_alongside_edge(edge_p1, edge_p2, segment_points,
                               max_dist_m: float = 15, max_angle_deg: float = 20) -> bool:
    """True if a road polyline both passes close to this edge AND runs
    roughly parallel to it — proximity alone isn't enough, since a road can
    pass near a lot corner (e.g. on a bend) without that edge actually
    fronting it, which produced a false positive on a real (non-corner) lot
    during testing."""
    edge_line = LineString([edge_p1, edge_p2])
    road_line = LineString(segment_points)
    dist_deg = edge_line.distance(road_line)
    if dist_deg * 111000 > max_dist_m:
        return False

    edge_dir = _unit_dir(edge_p1, edge_p2)
    road_dir = _unit_dir(segment_points[0], segment_points[-1])
    if edge_dir is None or road_dir is None:
        return False
    dot = max(-1.0, min(1.0, edge_dir[0] * road_dir[0] + edge_dir[1] * road_dir[1]))
    angle_deg = math.degrees(math.acos(abs(dot)))  # abs() folds anti-parallel to parallel
    return angle_deg <= max_angle_deg


def find_secondary_frontage_edges(
    ring: list, front_edge_idx: int, rear_edge_idx: int, n: int,
) -> list[int]:
    """
    Detect corner-lot edges: a non-front, non-rear edge that actually runs
    alongside a DIFFERENT public road than the front, rather than a
    neighbour's yard. A normal internal lot's side edges never border any
    street; a corner lot's second side runs close to AND roughly parallel
    with a genuinely different street's road segment.

    Costs one live NSW road-layer lookup per non-front/non-rear edge (2 for
    a typical rectangular lot), plus one for the front edge itself.
    """
    fp1, fp2 = ring[front_edge_idx], ring[(front_edge_idx + 1) % n]
    front_mid_lon = (fp1[0] + fp2[0]) / 2
    front_mid_lat = (fp1[1] + fp2[1]) / 2
    try:
        front_road_names = {
            s["name"] for s in get_road_segments_near_point(front_mid_lat, front_mid_lon, radius_m=15)
            if _road_runs_alongside_edge(fp1, fp2, s["points"])
        }
    except Exception:
        front_road_names = set()

    secondary = []
    for i in range(n):
        if i in (front_edge_idx, rear_edge_idx):
            continue
        p1 = ring[i]
        p2 = ring[(i + 1) % n]
        mid_lon = (p1[0] + p2[0]) / 2
        mid_lat = (p1[1] + p2[1]) / 2
        try:
            segments = get_road_segments_near_point(mid_lat, mid_lon, radius_m=15)
        except Exception:
            segments = []  # network hiccup — fail safe to "not a corner", don't block the envelope
        matches = [
            s["name"] for s in segments
            if s["name"] not in front_road_names and _road_runs_alongside_edge(p1, p2, s["points"])
        ]
        if matches:
            print(f"  Secondary frontage detected: edge {i} borders {matches[0]}")
            secondary.append(i)
    return secondary


def _apply_directional_setbacks(
    lot: Polygon,
    coords: list,
    front_edge_idx: int,
    front_setback_deg: float,
    rear_setback_deg: float,
    side_setback_deg: float,
    secondary_setback_deg: float | None = None,
    lonlat_ring: list | None = None,
    # Default OFF: direct testing against a real, verified non-corner lot
    # (35 Connecticut Avenue, Five Dock — a 6-sided/irregular block) produced
    # a false positive at 9.9m distance and within the parallelism tolerance,
    # because an internal boundary happened to run close to and roughly
    # parallel with the street's broader curve without being a real second
    # frontage. Distinguishing "genuine corner lot" from "coincidental
    # proximity" needs real cadastral frontage-count data, not inferred
    # road-centerline geometry — shipping this default-on risks silently
    # shrinking envelopes on ordinary irregular-shaped lots. Kept available
    # for opt-in / future use once validated against confirmed corner-lot
    # addresses; do not flip this default without doing that first.
    detect_corner_lots: bool = False,
) -> Polygon:
    """
    Clip the lot polygon by offsetting each edge inward by the correct setback.

    Algorithm: for each edge, build a half-plane (large rectangle on the inward
    side of the offset edge) and intersect the current result with it.
    This correctly separates front, rear, and side setbacks instead of averaging.

    Args:
        lot:               Shapely Polygon of the lot, in a local projected
                           metric coordinate system (see _project_to_m) —
                           all offsets below are applied as real metres.
        coords:            Ring points in that same projected system
                           (closing point included).
        front_edge_idx:    Index of the front edge start vertex (from get_front_edge).
        *_setback_deg:     Setback distances in metres (name kept for
                           backward compatibility with callers).
        secondary_setback_deg: Corner-lot secondary-frontage setback, if the
                           ruleset has one; falls back to side_setback_deg if
                           no corner lot is detected or this is None.
        lonlat_ring:       The same ring in real lon/lat degrees (not the
                           projected metric system `coords` is in) — needed
                           only by the opt-in detect_corner_lots path, which
                           calls the live NSW road-layer API and requires
                           real coordinates. Defaults to `coords` for
                           backward compatibility, which is only correct if
                           `coords` itself happens to already be lon/lat.

    Returns:
        (Shapely Polygon of the buildable area — may be MultiPolygon for odd
        lots — geometry_warnings): geometry_warnings flags cases where the
        front/rear/side edge model below is known to misfire (see the
        vertex-count check right after ring/n are computed) — the setback
        VALUES are still real DCP figures, but they may have been applied
        to the wrong physical edge, so the shape should be treated as
        unverified rather than silently trusted.
    """
    ring = coords[:-1]  # drop closing duplicate
    n = len(ring)
    geometry_warnings: list[dict] = []
    if n < 3:
        return lot, geometry_warnings

    # Confirmed failure mode: a 100-vertex lot (a curved/waterfront boundary
    # approximated by dozens of ~1m segments) had get_front_edge() pick a
    # 1.2m micro-segment as "the front edge" — meaningless for a setback,
    # since it isn't an actual building frontage. This model assumes a
    # simple ~4-8 sided lot; flag rather than silently apply setbacks to an
    # edge that was never a real frontage/boundary line.
    MAX_RELIABLE_VERTICES = 12
    if n > MAX_RELIABLE_VERTICES:
        geometry_warnings.append({
            "parameter": "geometry",
            "message": (
                f"This lot boundary has {n} vertices — front/rear/side setback "
                f"detection is designed for simple lots (~4-8 sides) and is "
                f"confirmed unreliable on complex or curved boundaries like this "
                f"one. Treat this envelope's shape as unverified; check the "
                f"actual site plan before relying on it."
            ),
        })

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

    # NOTE: an earlier version of this guard also flagged lots where the
    # detected rear edge isn't roughly parallel to the front edge (the
    # "furthest midpoint" heuristic can pick a side edge as rear on lots
    # wider than they are deep — confirmed concretely on a 43.8m-wide,
    # ~13m-deep lot). It was pulled back out: calibrating it against this
    # project's actual cached lots showed the vast majority present as
    # wide/shallow by this same measure — 53 of 59 simple (<=12-vertex)
    # cached lots came back >45 degrees off parallel — which would make the
    # warning fire on nearly every address rather than the rare case it was
    # meant to catch. Whether that's because Canada Bay's lot stock (as
    # resolved by get_lot_polygon) genuinely skews this way, or because
    # front-edge/orientation detection itself is unreliable for a lot of
    # real addresses, wasn't run down — that's a bigger investigation than
    # this guard was scoped for. Re-add only after that's actually answered.

    secondary_frontage_edges: list[int] = []
    if detect_corner_lots and secondary_setback_deg is not None:
        secondary_frontage_edges = find_secondary_frontage_edges(
            (lonlat_ring or coords)[:-1], front_edge_idx, rear_edge_idx, n
        )

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
        elif i in secondary_frontage_edges:
            sb = secondary_setback_deg
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
        # Extend 5km in every direction so it always covers the entire lot —
        # this used to be "2.0" when coords were raw lon/lat degrees (where a
        # lot spans ~0.0003°), which is a comically small 2 metres now that
        # coords are in a projected metric system; any residential lot would
        # exceed that, silently truncating the half-plane and corrupting the
        # intersection.
        BIG = 5000.0
        half_plane = Polygon([
            (op1[0] - ux * BIG,           op1[1] - uy * BIG),
            (op2[0] + ux * BIG,           op2[1] + uy * BIG),
            (op2[0] + ux * BIG + nx * BIG, op2[1] + uy * BIG + ny * BIG),
            (op1[0] - ux * BIG + nx * BIG, op1[1] - uy * BIG + ny * BIG),
        ])

        result = result.intersection(half_plane)
        if result.is_empty:
            break

    return result, geometry_warnings


CONTROL_PARAMETERS = {
    "fsr",
    "site_coverage_pct",
    "max_height",
    "max_storeys",
    "height_plane",
    "min_lot_size",
    "min_lot_width",
    "landscaped_area_pct",
    "landscaped_area_front_pct",
    "landscaped_area_rear_pct",
    "private_open_space",
    "private_open_space_min_dimension",
    "min_parking_spaces",
    "max_parking_spaces",
    "min_parking_per_dwelling",
    "car_space_length",
    "car_space_width",
}


def _area_sqm(geometry) -> float:
    """Area in m2, correcting for longitude compression at this geometry's
    own latitude (see _metric_scale above)."""
    sx, sy = _metric_scale(geometry.centroid.y)
    return round(geometry.area * sx * sy, 1)


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


def _first_rule_by_parameter(rules: list, parameter: str) -> dict | None:
    for rule in rules:
        if rule.get("parameter") == parameter:
            return rule
    return None


def _control_group(rules: list, parameters: list[str]) -> dict:
    return {
        param: _control_summary(_first_rule_by_parameter(rules, param))
        for param in parameters
    }


def compute_development_controls(
    lot_polygon: dict,
    envelope_polygon: dict,
    rules: list,
) -> dict:
    """
    Return non-spatial controls and derived numbers for an envelope response.

    These values do not change the buildable polygon. They expose controls like
    FSR, parking, height, landscaped area, and lot-size rules next to the
    geometry so callers can show the full rule context.
    """
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
            ["max_height", "max_storeys", "height_plane"],
        ),
        "parking": _control_group(
            rules,
            [
                "min_parking_spaces",
                "max_parking_spaces",
                "min_parking_per_dwelling",
                "car_space_length",
                "car_space_width",
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
        "lot_requirements": _control_group(
            rules,
            ["min_lot_size", "min_lot_width"],
        ),
        "missing_parameters": sorted(CONTROL_PARAMETERS - present_params),
    }


def compute_envelope_result(
    lot_polygon: dict,
    rules: list,
    geocoded_lat: float,
    geocoded_lon: float,
) -> dict:
    envelope, fallback_warnings = compute_envelope(lot_polygon, rules, geocoded_lat, geocoded_lon)
    return {
        "envelope": envelope,
        "fallback_warnings": fallback_warnings,
        "development_controls": compute_development_controls(
            lot_polygon,
            envelope,
            rules,
        ),
    }


def compute_envelope(lot_polygon: dict, rules: list,
                     geocoded_lat: float, geocoded_lon: float) -> tuple:
    """
    Given a lot polygon and a list of rules, compute the buildable envelope.

    lot_polygon:   GeoJSON Polygon {"type": "Polygon", "coordinates": [...]}
    rules:         list of rule dicts (e.g. from data/rules_r2_canada_bay.json)
    geocoded_lat:  raw geocoded latitude (lands on the road)
    geocoded_lon:  raw geocoded longitude (lands on the road)

    Returns (geojson_polygon, fallback_warnings) — fallback_warnings is a list
    of dicts, one per setback that had no matching rule and fell back to a
    generic placeholder value; empty if every setback came from a real rule.
    """
    coords = lot_polygon["coordinates"][0]
    lot = Polygon(coords)

    # Identify front edge using geocoded point (scale-invariant point-on-line
    # test, so this is fine to do in raw lon/lat before projecting).
    front_edge_idx, _ = get_front_edge(
        coords, geocoded_lon, geocoded_lat
    )

    # Project to a local metric coordinate system centred on this lot's own
    # latitude before doing ANY setback offset or area math (see
    # _metric_scale/_project_to_m above) — offsetting in raw lon/lat degrees
    # under-clips east/west-facing edges by ~17% at this latitude, since a
    # degree of longitude is a shorter physical distance than a degree of
    # latitude. Setback values are real metres in this space, no unit
    # conversion needed.
    coords_m = _project_to_m(coords, geocoded_lat)
    lot_m = Polygon(coords_m)
    lot_area_sqm = lot_m.area
    print(f"  Lot area: {lot_area_sqm:.1f} sqm")

    # Extract setback values from real rules
    front_setback      = None
    rear_setback       = None
    side_setback       = None
    side_setback_upper = None
    secondary_setback  = None  # corner-lot secondary-street side setback, if the DCP has one

    def _is_corner_condition(rule: dict) -> bool:
        text = " ".join(rule.get("conditions", [])).lower()
        return "secondary street" in text or "corner lot" in text or "corner" in text

    for rule in rules:
        param = rule.get("parameter")
        op    = rule.get("operator")

        if param == "front_setback" and op == "min":
            if front_setback is None:
                front_setback = rule["value"]
                print(f"  Front setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "rear_setback" and op == "min":
            if rear_setback is None:
                rear_setback = rule["value"]
                print(f"  Rear setback from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_ground" and op == "min":
            if _is_corner_condition(rule):
                if secondary_setback is None:
                    secondary_setback = rule["value"]
                    print(f"  Secondary-frontage (corner lot) side setback from rule: "
                          f"{rule['value']}m (clause {rule.get('source_clause', 'unknown')})")
            elif side_setback is None:
                side_setback = rule["value"]
                print(f"  Side setback (ground) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

        elif param == "side_setback_upper" and op == "min":
            if side_setback_upper is None:
                side_setback_upper = rule["value"]
                print(f"  Side setback (upper) from rule: {rule['value']}m "
                      f"(clause {rule.get('source_clause', 'unknown')})")

    # Fall back to Canada Bay DCP R2 defaults only if rule not found. Every
    # fallback used is collected in `fallback_warnings` and returned to the
    # caller — this shape is only as good as the setbacks that produced it,
    # and a silent made-up number looks identical to a real one unless the
    # caller (and ultimately the end user) is told which is which.
    fallback_warnings = []
    if front_setback is None:
        msg = "No front setback rule matched this lot/zone — used a generic placeholder of 4.5m instead of a real council figure."
        print(f"  WARNING: {msg}")
        fallback_warnings.append({"parameter": "front_setback", "placeholder_value_m": 4.5, "message": msg})
        front_setback = 4.5
    if rear_setback is None:
        msg = "No rear setback rule matched this lot/zone — used a generic placeholder of 4.0m instead of a real council figure."
        print(f"  WARNING: {msg}")
        fallback_warnings.append({"parameter": "rear_setback", "placeholder_value_m": 4.0, "message": msg})
        rear_setback = 4.0
    if side_setback is None:
        msg = "No side setback (ground floor) rule matched this lot/zone — used a generic placeholder of 0.9m instead of a real council figure."
        print(f"  WARNING: {msg}")
        fallback_warnings.append({"parameter": "side_setback_ground", "placeholder_value_m": 0.9, "message": msg})
        side_setback = 0.9
    if side_setback_upper is None:
        msg = "No side setback (upper floor) rule matched this lot/zone — used a generic placeholder of 1.5m instead of a real council figure."
        print(f"  WARNING: {msg}")
        fallback_warnings.append({"parameter": "side_setback_upper", "placeholder_value_m": 1.5, "message": msg})
        side_setback_upper = 1.5

    print(f"  Setbacks — front: {front_setback:.1f}m, "
          f"rear: {rear_setback:.1f}m, "
          f"side (ground): {side_setback:.1f}m, "
          f"side (upper): {side_setback_upper:.1f}m")

    # Apply directional setbacks: clip the lot with a half-plane per edge.
    # Each edge is offset inward by its specific setback (front / rear / side).
    # This replaces the previous uniform average buffer which was inaccurate
    # by up to 50% for lots where front >> side (4.5m vs 0.9m in Canada Bay R2).
    # front_edge_idx already computed above by get_front_edge. A corner lot's
    # secondary-street-facing side gets secondary_setback instead of the
    # plain side_setback, detected live against the NSW road layer.
    # All of this happens in the projected metric polygon/coords, so `sb`
    # values below are real metres, not degrees.
    envelope_m, geometry_warnings = _apply_directional_setbacks(
        lot_m, coords_m, front_edge_idx,
        front_setback, rear_setback, side_setback,
        secondary_setback_deg=secondary_setback,
        lonlat_ring=coords,
    )
    fallback_warnings.extend(geometry_warnings)

    if envelope_m is None or envelope_m.is_empty:
        raise ValueError("Lot is too small for the required setbacks!")

    envelope_area_sqm = envelope_m.area
    print(f"  Buildable envelope area: {envelope_area_sqm:.1f} sqm")
    print(f"  Coverage: {(envelope_area_sqm/lot_area_sqm)*100:.1f}% of lot")

    envelope = _unproject_geometry(envelope_m, geocoded_lat)
    return mapping(envelope), fallback_warnings


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

        envelope_result = compute_envelope_result(polygon, rules, lat, lon)

        results[address] = {
            "lot_polygon":   polygon,
            "envelope":      envelope_result["envelope"],
            "development_controls": envelope_result["development_controls"],
            "rules_applied": rules
        }
        print()

    with open("data/envelopes.json", "w") as f:
        json.dump(results, f, indent=2)

    print("Done! Saved to data/envelopes.json")
