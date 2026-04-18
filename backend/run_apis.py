"""
run_apis.py — Ground-truth lot cache builder for NSW addresses.

Input: 
Reads addresses from data/ground_truth_addresses.csv and fetches each lot's
polygon geometry and zoning classification from NSW government spatial APIs.

Output:
Results are cached in data/cached_lots.json so downstream scripts (check_lmr.py,
compute_envelope.py) can run without live API calls. 

Process: Two fetch strategies are used: 
1) direct cadastre address matching (preferred)
2) geocode-then-lookup fallback. 

The cache also records expected vs actual zone for mismatch detection.
Run with --refresh to force re-fetch all entries; otherwise only missing addresses are fetched. 

"""

import csv
import json
import re
import sys
import time
import argparse
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = ROOT_DIR / "data"

sys.path.insert(0, str(BASE_DIR))

from nsw_apis import geocode, get_lot_polygon, get_zone

GROUND_TRUTH_PATH = DATA_DIR / "ground_truth_addresses.csv"
CACHE_PATH = DATA_DIR / "cached_lots.json"
CADASTRE_URL = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/"
    "NSW_Land_Parcel_Property_Theme/FeatureServer/12/query"
)


# Parse the ground-truth CSV into deduplicated address records with suburb, LGA, and expected zone.
def load_ground_truth_rows() -> list[dict]:
    with open(GROUND_TRUTH_PATH, newline="", encoding="utf-8-sig") as f:
        rows = csv.DictReader(f)
        records = []
        seen = set()
        for row in rows:
            address = row.get("address", "").strip()
            if not address or address in seen:
                continue
            seen.add(address)
            records.append({
                "address": address,
                "suburb": row.get("suburb", "").strip(),
                "lga": row.get("lga", "").strip(),
                "expected_zone": row.get("zone", "").strip(),
            })
    return records


# Load the existing JSON cache from disk, returning an empty dict if the file doesn't exist.
def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return {key.strip(): value for key, value in raw.items()}


# Write the cache dict to disk as sorted, indented JSON.
def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


# Merge ground-truth metadata (suburb, LGA, expected zone, mismatch flag) into a lot data dict.
def with_ground_truth_metadata(lot_data: dict, row: dict) -> dict:
    expected_zone = row.get("expected_zone")
    actual_zone = lot_data.get("zone")
    source = lot_data.get("source") or "existing_cache"
    return {
        **lot_data,
        "suburb": row.get("suburb", ""),
        "lga": row.get("lga", ""),
        "expected_zone": expected_zone,
        "zone_mismatch": bool(expected_zone and actual_zone and expected_zone != actual_zone),
        "source": source,
    }


# Strip the trailing 4-digit postcode and uppercase the address to match cadastre formatting.
def address_without_postcode(address: str) -> str:
    return re.sub(r"\s+\d{4}$", "", address.strip()).upper()


# Compute the simple average centroid (lat, lon) of a GeoJSON polygon's outer ring.
def polygon_centroid(polygon: dict) -> tuple[float, float]:
    ring = polygon["coordinates"][0]
    lon = sum(point[0] for point in ring) / len(ring)
    lat = sum(point[1] for point in ring) / len(ring)
    return lat, lon


# Query the NSW Cadastre FeatureServer by address string, pick the largest-area match, and return its polygon and zone.
def fetch_lot_from_cadastre_address(row: dict) -> dict | None:
    cadastre_address = address_without_postcode(row["address"])
    escaped = cadastre_address.replace("'", "''")
    params = {
        "where": f"address = '{escaped}'",
        "outFields": "address,housenumber,Shape__Area",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 10,
    }
    response = requests.get(CADASTRE_URL, params=params, timeout=30)
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        return None

    def area(feature: dict) -> float:
        try:
            return float(feature.get("attributes", {}).get("Shape__Area") or 0)
        except (TypeError, ValueError):
            return 0

    best = max(features, key=area)
    rings = best.get("geometry", {}).get("rings", [])
    if not rings:
        return None

    polygon = {"type": "Polygon", "coordinates": rings}
    lat, lon = polygon_centroid(polygon)
    print(f"  Cadastre match: {best.get('attributes', {}).get('address', cadastre_address)}")
    zone = get_zone(lat, lon, polygon=polygon)
    return with_ground_truth_metadata({
        "lat": lat,
        "lon": lon,
        "zone": zone,
        "polygon": polygon,
        "source": "nsw_cadastre_address",
    }, row)


# Fetch lot data using cadastre address match first, falling back to geocode + spatial lookup if that fails.
def fetch_lot(row: dict) -> dict:
    address = row["address"]
    cadastre_lot = fetch_lot_from_cadastre_address(row)
    if cadastre_lot:
        return cadastre_lot

    print("  Cadastre address match not found; falling back to geocode lookup")
    lat, lon = geocode(address)
    time.sleep(0.5)
    polygon = get_lot_polygon(lat, lon)
    time.sleep(0.5)
    zone = get_zone(lat, lon, polygon=polygon)
    return with_ground_truth_metadata({
        "lat": lat,
        "lon": lon,
        "zone": zone,
        "polygon": polygon,
        "source": "geocode_then_cadastre",
    }, row)


# Entry point: iterate all ground-truth addresses, fetch/cache missing lots, report zone mismatches and failures.
def main() -> int:
    parser = argparse.ArgumentParser(description="Cache lot polygons for ground-truth addresses.")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refetch every ground-truth address instead of only missing cache entries.",
    )
    args = parser.parse_args()

    rows = load_ground_truth_rows()
    addresses = [row["address"] for row in rows]
    cache = load_cache()
    cache = {address: cache[address] for address in addresses if address in cache}

    print(f"Ground truth addresses: {len(rows)}")
    print(f"Cached lots before: {len(cache)}")

    failures = []
    for index, row in enumerate(rows, start=1):
        address = row["address"]
        if address in cache and not args.refresh:
            cache[address] = with_ground_truth_metadata(cache[address], row)
            print(f"[{index}/{len(addresses)}] cached: {address}")
            continue

        print(f"[{index}/{len(addresses)}] fetching: {address}")
        try:
            cache[address] = fetch_lot(row)
            save_cache(cache)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failures.append({"address": address, "error": str(exc)})

        time.sleep(1.0)

    save_cache(cache)
    print(f"Cached lots after: {len(cache)}")

    mismatches = [
        (address, lot.get("expected_zone"), lot.get("zone"))
        for address, lot in cache.items()
        if lot.get("zone_mismatch")
    ]
    if mismatches:
        print("\nZone mismatches:")
        for address, expected, actual in mismatches:
            print(f"  {address}: expected {expected}, live {actual}")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  {failure['address']}: {failure['error']}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())