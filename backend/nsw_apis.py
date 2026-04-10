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
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "au"
    }
    headers = {"User-Agent": "AutomatedComplianceChecker/1.0"}
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    results = response.json()

    if not results:
        raise ValueError(f"Could not geocode address: {address}")

    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])
    print(f"  Geocoded: {address} -> ({lat}, {lon})")
    return lat, lon


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
        "outFields": "LAY_CLASS",
        "returnGeometry": "false",
        "f": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    features = data.get("features", [])
    if not features:
        raise ValueError(f"No zone found at ({lat}, {lon})")

    zone_name = features[0]["attributes"]["LAY_CLASS"]
    zone_code = ZONE_NAME_TO_CODE.get(zone_name, zone_name)
    print(f"  Zone: {zone_code} ({zone_name})")
    return zone_code