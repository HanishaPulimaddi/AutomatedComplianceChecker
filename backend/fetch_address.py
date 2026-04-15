import requests
import json
import pandas as pd
import os
import time

CADASTRE_URL = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/"
    "NSW_Land_Parcel_Property_Theme/FeatureServer/12/query"
)
ZONING_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
    "Planning/EPI_Primary_Planning_Layers/MapServer/2/query"
)
HERITAGE_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
    "Planning/EPI_Primary_Planning_Layers/MapServer/9/query"
)
FORESHORE_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
    "Planning/EPI_Primary_Planning_Layers/MapServer/11/query"
)
ACID_SULFATE_URL = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
    "Planning/EPI_Primary_Planning_Layers/MapServer/1/query"
)

# Output paths — relative to project root
# Run this script from C:\AutomatedComplianceChecker
OUTPUT_FULL  = "data/r2_canada_bay_addresses.csv"
OUTPUT_CLEAN = "data/r2_canada_bay_clean.csv"
OUTPUT_TEST  = "data/r2_canada_bay_test_addresses.csv"

SUBURBS = [
    "FIVE DOCK",
    "DRUMMOYNE",
    "CONCORD",
    "ABBOTSFORD",
    "RUSSELL LEA",
    "WAREEMBA",
    "RHODES",
    "CABARITA",
]

MAX_PER_SUBURB = 1000


def fetch_suburb(suburb):
    print(f"  Fetching {suburb}...")
    all_features = []
    offset = 0
    page_size = 1000

    while True:
        params = {
            "where": f"address LIKE '%{suburb}%'",
            "outFields": "address, housenumber, Shape__Area",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultRecordCount": page_size,
            "resultOffset": offset,
        }

        exceeded = False
        for attempt in range(3):
            try:
                resp = requests.get(CADASTRE_URL, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                features = data.get("features", [])
                exceeded = data.get("exceededTransferLimit", False)
                break
            except Exception as e:
                print(f"    Attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    print(f"    Retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    print(f"    All 3 attempts failed for {suburb}. Skipping page.")
                    return all_features

        if not features:
            break

        all_features.extend(features)

        if len(all_features) >= MAX_PER_SUBURB:
            print(f"    Hit {MAX_PER_SUBURB} limit for {suburb} — stopping.")
            break

        if not exceeded:
            break

        offset += page_size
        time.sleep(1)

    print(f"    {suburb}: {len(all_features)} parcels fetched")
    return all_features


def fetch_all_properties():
    print("Step 1: Fetching properties per suburb (up to 1000 each)...")
    all_features = []

    for suburb in SUBURBS:
        features = fetch_suburb(suburb)
        all_features.extend(features)
        time.sleep(1)

    print(f"\n  Total parcels fetched: {len(all_features)}")
    return all_features


def parse_properties(features):
    print("\nStep 2: Parsing into clean rows...")
    rows = []

    for f in features:
        props = f.get("attributes", {})
        geom  = f.get("geometry", {})

        address = props.get("address", "")
        if not address or address.strip() == "":
            continue

        try:
            area_sqm = round(float(props.get("Shape__Area", 0)), 1)
        except (ValueError, TypeError):
            area_sqm = None

        if not area_sqm or area_sqm < 200:
            continue

        address_upper = address.strip().upper()
        suburb = ""
        for sub in SUBURBS:
            if address_upper.endswith(sub):
                suburb = sub.title()
                break

        if not suburb:
            continue

        if "/" in address:
            continue

        rings = geom.get("rings", [])
        centroid_lon, centroid_lat, n_vertices = None, None, 0
        if rings:
            ring = rings[0]
            n_vertices = len(ring)
            lons = [pt[0] for pt in ring]
            lats = [pt[1] for pt in ring]
            centroid_lon = round(sum(lons) / len(lons), 6)
            centroid_lat = round(sum(lats) / len(lats), 6)

        if not centroid_lat or not centroid_lon:
            continue

        is_corner = n_vertices >= 6

        if area_sqm < 450:
            size_flag = "small"
        elif area_sqm > 2000:
            size_flag = "large"
        else:
            size_flag = "normal"

        rows.append({
            "address":       address.title(),
            "suburb":        suburb,
            "lot_area_sqm":  area_sqm,
            "centroid_lat":  centroid_lat,
            "centroid_lon":  centroid_lon,
            "is_corner_lot": is_corner,
            "n_vertices":    n_vertices,
            "size_flag":     size_flag,
        })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["address"])
    df = df.sort_values(["suburb", "address"]).reset_index(drop=True)

    print(f"  Clean addresses after parsing: {len(df)}")
    print("\n  Count per suburb:")
    print(df.groupby("suburb")["address"].count().to_string())
    return df


def check_zone(lat, lon):
    for attempt in range(3):
        try:
            params = {
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "SYM_CODE",
                "returnGeometry": "false",
                "f": "json",
            }
            resp = requests.get(ZONING_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            features = data.get("features", [])
            if features:
                return features[0].get("attributes", {}).get("SYM_CODE", "")
            return ""
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"    Warning: zone check failed for ({lat},{lon}): {e}")
                return ""


def check_overlay(lat, lon, url, label):
    for attempt in range(3):
        try:
            params = {
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "OBJECTID",
                "returnGeometry": "false",
                "returnCountOnly": "true",
                "f": "json",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            count = data.get("count", 0)
            return count > 0
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"    Warning: {label} check failed for ({lat},{lon}): {e}")
                return False


def run_overlay_checks(df):
    print(f"\nStep 3: Running overlay checks on {len(df)} candidates...")
    print("  Checks: zone, heritage, foreshore, acid sulfate (flag only).")
    print("  Flood + land reservation skipped — known false positives.")
    estimated = round(len(df) * 4 * 0.15 / 60, 1)
    print(f"  Estimated time: {estimated} minutes")

    results = []
    total = len(df)

    for i, row in df.iterrows():
        lat = row["centroid_lat"]
        lon = row["centroid_lon"]

        if (i + 1) % 20 == 0:
            print(f"  Checking {i+1}/{total}...")

        zone = check_zone(lat, lon)
        time.sleep(0.15)

        if zone != "R2":
            results.append({
                "zone":            zone,
                "is_heritage":     False,
                "is_foreshore":    False,
                "is_acid_sulfate": False,
                "drop_reason":     f"wrong_zone:{zone}",
                "verified_clean":  False,
            })
            continue

        is_heritage     = check_overlay(lat, lon, HERITAGE_URL,  "heritage")
        time.sleep(0.15)
        is_foreshore    = check_overlay(lat, lon, FORESHORE_URL, "foreshore")
        time.sleep(0.15)
        is_acid_sulfate = check_overlay(lat, lon, ACID_SULFATE_URL, "acid_sulfate")
        time.sleep(0.15)

        drop_reason = ""
        if is_heritage:
            drop_reason = "heritage"
        elif is_foreshore:
            drop_reason = "foreshore"

        results.append({
            "zone":            zone,
            "is_heritage":     is_heritage,
            "is_foreshore":    is_foreshore,
            "is_acid_sulfate": is_acid_sulfate,
            "drop_reason":     drop_reason,
            "verified_clean":  drop_reason == "",
        })

    overlay_df = pd.DataFrame(results)
    df = pd.concat([df.reset_index(drop=True), overlay_df], axis=1)

    dropped = df[df["drop_reason"] != ""]
    clean   = df[df["verified_clean"] == True]

    print(f"\n  Total checked:  {len(df)}")
    print(f"  Clean (R2, no overlays): {len(clean)}")
    print(f"  Dropped: {len(dropped)}")

    if len(dropped) > 0:
        print("\n  Drop reasons:")
        print(dropped["drop_reason"].value_counts().to_string())

    return df, clean


def make_test_set(clean_df):
    print("\nStep 4: Selecting test set (5 per suburb)...")
    test_rows = []

    for suburb, group in clean_df.groupby("suburb"):
        if not suburb:
            continue

        normal = group[group["size_flag"] == "normal"]
        if len(normal) < 5:
            normal = group

        n_corners  = min(2, len(normal[normal["is_corner_lot"]]))
        n_regulars = min(3, len(normal[~normal["is_corner_lot"]]))

        corners  = normal[normal["is_corner_lot"]].sample(n_corners,  random_state=42) if n_corners  > 0 else pd.DataFrame()
        regulars = normal[~normal["is_corner_lot"]].sample(n_regulars, random_state=42) if n_regulars > 0 else pd.DataFrame()

        sample = pd.concat([corners, regulars]).head(5)
        test_rows.append(sample)
        print(f"  {suburb}: {len(sample)} selected")

    if not test_rows:
        print("ERROR: No suburbs found in clean data.")
        return pd.DataFrame()

    return pd.concat(test_rows).reset_index(drop=True)


def main():
    # Make sure data folder exists
    os.makedirs("data", exist_ok=True)

    # Step 1 — fetch per suburb
    features = fetch_all_properties()
    if not features:
        print("ERROR: No properties returned.")
        return

    # Step 2 — parse
    df = parse_properties(features)
    if df.empty:
        print("ERROR: No clean addresses after parsing.")
        return

    # Save full list immediately — before overlay checks
    df.to_csv(OUTPUT_FULL, index=False)
    print(f"\n  Full list saved → {OUTPUT_FULL} ({len(df)} addresses)")

    # Step 3 — overlay checks on top 50 normal lots per suburb
    candidates = []
    for suburb, group in df.groupby("suburb"):
        sample = group[group["size_flag"] == "normal"].head(50)
        if len(sample) < 10:
            sample = group.head(50)
        candidates.append(sample)
    candidates_df = pd.concat(candidates).reset_index(drop=True)

    print(f"\n  Checking {len(candidates_df)} candidates (top 50 per suburb)")

    full_checked, clean_df = run_overlay_checks(candidates_df)

    if clean_df.empty:
        print("ERROR: No clean addresses after overlay checks.")
        print(f"  Full unfiltered list still saved at {OUTPUT_FULL}")
        return

    # Save clean list
    clean_df.to_csv(OUTPUT_CLEAN, index=False)
    print(f"\n  Clean list saved → {OUTPUT_CLEAN} ({len(clean_df)} addresses)")

    # Step 4 — test set
    test_df = make_test_set(clean_df)

    if not test_df.empty:
        test_df.to_csv(OUTPUT_TEST, index=False)
        print(f"  Test set saved → {OUTPUT_TEST} ({len(test_df)} addresses)")

        print("\n── Final verified test addresses ───────────────────")
        for _, row in test_df.iterrows():
            corner = " [CORNER]"       if row["is_corner_lot"]   else ""
            acid   = " [ACID SULFATE]" if row["is_acid_sulfate"] else ""
            print(f"  {row['address']}{corner}{acid}  ({int(row['lot_area_sqm'])}sqm)")

    print("\n── Summary by suburb ───────────────────────────────")
    summary = clean_df.groupby("suburb").agg(
        clean_lots=("address", "count"),
        corner_lots=("is_corner_lot", "sum"),
        avg_lot_sqm=("lot_area_sqm", "mean")
    ).round(0)
    print(summary.to_string())

    print("\nDone.")


if __name__ == "__main__":
    main()