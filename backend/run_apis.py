import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
from nsw_apis import geocode, get_lot_polygon, get_zone

addresses = [
    "35 Connecticut Avenue Five Dock 2046",
    "28 Clare Crescent Russell Lea 2046",
    "18 Spring Street Abbotsford 2046",
]

results = {}

for addr in addresses:
    print(f"\nProcessing: {addr}")
    lat, lon = geocode(addr)
    polygon = get_lot_polygon(lat, lon)
    zone = get_zone(lat, lon)

    results[addr] = {
        "lat": lat,
        "lon": lon,
        "zone": zone,
        "polygon": polygon
    }
    time.sleep(1)

os.makedirs("data", exist_ok=True)
with open("data/cached_lots.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nDone! Saved to data/cached_lots.json")