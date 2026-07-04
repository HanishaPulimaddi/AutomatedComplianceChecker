import requests
import json
import os
import time

ZONE_NAME_TO_CODE = {
    "Large Lot Residential": "R1",
    "Low Density Residential": "R2",
    "Medium Density Residential": "R3",
    "High Density Residential": "R4",
    "Mixed Use": "B4",
    "Local Centre": "B1",
    "Neighbourhood Centre": "B1",
    "Village": "RU5",
    "General Industrial": "IN1",
    "Light Industrial": "IN2",
    "Heavy Industrial": "IN3",
    "Business Park": "B7",
    "Metropolitan Centre": "B3",
    "Commercial Core": "B3",
    "Enterprise Corridor": "B6",
    "Environmental Conservation": "E1",
    "Environmental Management": "E3",
    "Environmental Living": "E4",
    "National Parks and Nature Reserves": "E1",
    "Primary Production": "RU1",
    "Rural Landscape": "RU2",
    "Forestry": "RU3",
    "Primary Production Small Lots": "RU4",
    "Recreation": "RE1",
    "Private Recreation": "RE2",
    "Public Recreation": "RE1",
    "Special Activities": "SP1",
    "Infrastructure": "SP2",
    "Tourist": "SP3",
    "Waterway": "W1",
    "Natural Waterways": "W2",
    "Working Waterfront": "W3",
    "Unzoned": "UZ",
}


def geocode(address: str) -> tuple[float, float]:
    # ArcGIS World Geocoder — authoritative Australian address data, no API key needed
    url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
    params = {
        "SingleLine": address,
        "countryCode": "AUS",
        "maxLocations": 1,
        "outFields": "",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    candidates = response.json().get("candidates", [])

    if not candidates:
        raise ValueError(f"Could not geocode address: {address}")

    loc = candidates[0]["location"]
    lat, lon = loc["y"], loc["x"]
    print(f"  Geocoded: {address} -> ({lat}, {lon})")
    return lat, lon


# Roughly the Canada Bay LGA bounding box — used to bias autocomplete
# suggestions toward addresses this app can actually check, since it only
# supports one council. Doesn't exclude results outside it, just ranks
# nearby matches higher.
_CANADA_BAY_BOUNDS = "151.06,-33.87,151.16,-33.80"
_CANADA_BAY_CENTRE = "151.10,-33.85"


def get_address_suggestions(text: str, max_suggestions: int = 6) -> list[str]:
    """
    Autocomplete-as-you-type suggestions from the same ArcGIS World Geocoder
    used for geocode() — no separate API/key, and it's built to handle messy,
    abbreviated, partially-typed input (tested: "110 correys ave conc" ->
    "110 Correys Avenue, Concord, Sydney, New South Wales, 2137, AUS").

    ArcGIS's suggest endpoint mixes real street addresses in with points of
    interest — sports clubs, ovals, shops, churches — and for a bare suburb
    name (e.g. "drummoyne") the POIs can crowd out every genuine address, or
    there may be no address suggestion at all yet. Selecting a POI resolves
    to a real set of coordinates (a park, a boat shed) that then correctly
    gets rejected as an unsupported zone — technically correct, but
    confusing, since the user was never trying to look up a sailing club.
    Real NSW street addresses always start with a house number, so filtering
    to only "starts with a digit" keeps genuine addresses and drops POIs
    without needing a fragile text/category match on ArcGIS's side (which
    doesn't reliably work for this dataset — tested and it returns nothing).
    """
    if not text or len(text.strip()) < 3:
        return []
    url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/suggest"
    params = {
        "text": text,
        "countryCode": "AUS",
        "f": "json",
        # Ask for more than we'll show — POIs get filtered out below, so
        # requesting only `max_suggestions` up front would often leave
        # nothing once non-address results are dropped.
        "maxSuggestions": max_suggestions * 3,
        "location": _CANADA_BAY_CENTRE,
        "searchExtent": _CANADA_BAY_BOUNDS,
    }
    response = requests.get(url, params=params, timeout=8)
    response.raise_for_status()
    suggestions = response.json().get("suggestions", [])
    addresses = [
        s["text"] for s in suggestions
        if not s.get("isCollection") and s["text"][:1].isdigit()
    ]
    return addresses[:max_suggestions]


def get_lot_polygon(lat: float, lon: float) -> dict:
    url = (
        "https://maps.six.nsw.gov.au/arcgis/rest/services"
        "/sixmaps/Cadastre/MapServer/0/query"
    )

    for delta in [0.0001, 0.0002, 0.0005, 0.001]:
        params = {
            "geometry": f"{lon-delta},{lat-delta},{lon+delta},{lat+delta}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        }
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        features = data.get("features", [])
        if features:
            def centroid_dist(feat):
                rings = feat["geometry"]["rings"]
                xs = [p[0] for p in rings[0]]
                ys = [p[1] for p in rings[0]]
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                return (cx - lon) ** 2 + (cy - lat) ** 2

            best = min(features, key=centroid_dist)
            rings = best["geometry"]["rings"]
            polygon = {"type": "Polygon", "coordinates": rings}
            print(f"  Lot polygon found: {len(rings[0])} points "
                  f"(delta={delta}, {len(features)} candidate(s))")
            return polygon

    raise ValueError(f"No lot found at ({lat}, {lon})")

def get_fsr_from_map(lat: float, lon: float) -> float | None:
    """Return the LEP FSR value for a point from the NSW Planning FSR Map layer."""
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/1/query"
    )
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FSR,LAY_CLASS",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    features = response.json().get("features", [])
    # Pick the feature with a numeric FSR value (skip "CA" / null entries)
    for feat in features:
        fsr = feat["attributes"].get("FSR")
        if fsr is not None:
            print(f"  FSR from map: {fsr} ({feat['attributes'].get('LAY_CLASS')})")
            return float(fsr)
    print("  FSR from map: not found")
    return None


def get_hob_from_map(lat: float, lon: float) -> float | None:
    """Return the LEP max building height (metres) for a point from the
    NSW Planning Height of Building Map layer. Unlike Canada Bay's flat
    8.5m DCP figure for low/medium density dwellings, this varies hugely
    for R4 residential flat buildings (seen 25m-82m within the same
    precinct) — Part F's own height table scales storeys off this map
    value rather than stating a fixed cap for that dwelling type."""
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/5/query"
    )
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "MAX_B_H_M,LAY_CLASS",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    features = response.json().get("features", [])
    for feat in features:
        hob = feat["attributes"].get("MAX_B_H_M")
        if hob is not None:
            print(f"  HOB from map: {hob}m ({feat['attributes'].get('LAY_CLASS')})")
            return float(hob)
    print("  HOB from map: not found")
    return None


def get_min_lot_size_from_map(lat: float, lon: float) -> float | None:
    """Return the LEP minimum lot size (m2) for a point from the NSW
    Planning Lot Size Map layer. Spatially variable like FSR/height —
    not stated in Canada Bay's DCP text at all, so this is the only
    source for it."""
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/4/query"
    )
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "LOT_SIZE,LAY_CLASS",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    features = response.json().get("features", [])
    for feat in features:
        lot_size = feat["attributes"].get("LOT_SIZE")
        if lot_size is not None:
            print(f"  Min lot size from map: {lot_size}m2 ({feat['attributes'].get('LAY_CLASS')})")
            return float(lot_size)
    print("  Min lot size from map: not found")
    return None


def get_heritage_from_map(lat: float, lon: float) -> dict | None:
    """Return heritage listing info for a point from the NSW Planning
    Heritage Map layer, or None if the lot isn't heritage-listed.
    Overlay/categorical, not a numeric substitution like FSR/height/lot
    size — used to flag when Part C Heritage controls may apply."""
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/0/query"
    )
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "H_NAME,SIG,LAY_CLASS",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    features = response.json().get("features", [])
    for feat in features:
        attrs = feat["attributes"]
        if attrs.get("H_NAME") or attrs.get("LAY_CLASS"):
            print(f"  Heritage: {attrs.get('H_NAME')} ({attrs.get('SIG')})")
            return {"name": attrs.get("H_NAME"), "significance": attrs.get("SIG"),
                     "category": attrs.get("LAY_CLASS")}
    return None


def get_roads_near_point(lat: float, lon: float, radius_m: float = 25) -> list[str]:
    """Return road names (e.g. "Barnstaple Road") within radius_m of a point,
    from the NSW Transport Theme RoadSegment layer. Used to detect corner
    lots (a lot edge running alongside a second street needs the DCP's
    bigger secondary-frontage setback, not the plain side setback) and to
    check DCP carve-outs that are defined by named road boundaries rather
    than a mapped zone (e.g. Part F excludes R3 land fronting specific
    named roads near Five Dock Town Centre, governed by Part G instead)."""
    url = "https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer/5/query"
    d = radius_m / 111000  # rough metres -> degrees
    params = {
        "geometry": f"{lon-d},{lat-d},{lon+d},{lat+d}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "roadnamebase,roadnametype,roadnamesuffix",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    names = set()
    for feat in response.json().get("features", []):
        a = feat["attributes"]
        base = a.get("roadnamebase")
        if not base:
            continue
        parts = [base, a.get("roadnametype"), a.get("roadnamesuffix")]
        names.add(" ".join(p for p in parts if p).title())
    return sorted(names)


def get_road_segments_near_point(lat: float, lon: float, radius_m: float = 25) -> list[dict]:
    """Return road segments (name + polyline vertices) within radius_m of a
    point. Unlike get_roads_near_point, keeps geometry so a caller can check
    whether a road actually runs ALONGSIDE a specific lot edge (roughly
    parallel, close) rather than merely existing somewhere nearby — a road
    can pass near a lot corner without that lot edge actually fronting it."""
    url = "https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer/5/query"
    d = radius_m / 111000
    params = {
        "geometry": f"{lon-d},{lat-d},{lon+d},{lat+d}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "roadnamebase,roadnametype,roadnamesuffix",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    segments = []
    for feat in response.json().get("features", []):
        a = feat["attributes"]
        base = a.get("roadnamebase")
        paths = feat.get("geometry", {}).get("paths", [])
        if not base or not paths:
            continue
        parts = [base, a.get("roadnametype"), a.get("roadnamesuffix")]
        name = " ".join(p for p in parts if p).title()
        for path in paths:
            segments.append({"name": name, "points": path})
    return segments


def get_lga(lat: float, lon: float) -> str:
    """Return the LGA name for a point using the NSW Planning API."""
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/2/query"
    )
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "LGA_NAME",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        raise ValueError(f"Could not determine LGA at ({lat}, {lon})")
    lga = features[0]["attributes"].get("LGA_NAME", "")
    print(f"  LGA: {lga}")
    return lga


def get_zone(lat: float, lon: float, polygon: dict = None) -> str:
    url = (
        "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services"
        "/Planning/EPI_Primary_Planning_Layers/MapServer/2/query"
    )

    if polygon:
        rings = polygon["coordinates"][0]
        xs = [p[0] for p in rings]
        ys = [p[1] for p in rings]
        lon = sum(xs) / len(xs)
        lat = sum(ys) / len(ys)
        print(f"  Using polygon centroid for zone query: ({lat:.6f}, {lon:.6f})")

    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "LAY_CLASS,SYM_CODE",
        "returnGeometry": "false",
        "f": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    features = data.get("features", [])
    if not features:
        raise ValueError(f"No zone found at ({lat}, {lon})")

    attrs = features[0]["attributes"]
    zone_name = attrs["LAY_CLASS"]
    # SYM_CODE is the map's own current zone code (e.g. "MU1") — use it
    # directly rather than translating the descriptive LAY_CLASS name
    # through a hardcoded table. That table used pre-2023 codes (e.g.
    # "Mixed Use" -> "B4"); NSW's 2023 zone-code reform renamed several
    # zones (B4 -> MU1, B1/B2 -> E1, etc.) and the live data already
    # reflects it (SYM_CODE="MU1" for a lot whose LAY_CLASS is still the
    # unchanged descriptive text "Mixed Use") — residential codes (R1-R5)
    # were not part of that rename, so this only affects non-residential
    # zone labels, but those were being reported under a deprecated code.
    zone_code = attrs.get("SYM_CODE") or ZONE_NAME_TO_CODE.get(zone_name, zone_name)
    print(f"  Zone: {zone_code} ({zone_name})")
    return zone_code