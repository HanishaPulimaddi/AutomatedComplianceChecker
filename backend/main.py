import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from nsw_apis import geocode, get_lot_polygon, get_zone
from compute_envelope import compute_envelope

app = FastAPI(title="Automated Compliance Checker", version="1.0.0")

# CORS - allows the React web app at localhost:5173 to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load verified rules once at startup
with open("data/rules_r2_canada_bay.json") as f:
    ALL_RULES = json.load(f)

RULES = [r for r in ALL_RULES if r.get("confidence", 0) >= 0.8]
print(f"Loaded {len(RULES)} rules at startup")


# ── Request models ──────────────────────────────────────────

class SiteRequest(BaseModel):
    address: str

class EnvelopeRequest(BaseModel):
    address: str


# ── Helper: check cache first ────────────────────────────────

def get_cached_lot(address: str):
    try:
        with open("data/cached_lots.json") as f:
            cache = json.load(f)
        if address in cache:
            print(f"  Cache hit: {address}")
            return cache[address]
    except FileNotFoundError:
        pass
    return None

def save_to_cache(address: str, lot_data: dict):
    try:
        with open("data/cached_lots.json") as f:
            cache = json.load(f)
    except FileNotFoundError:
        cache = {}
    cache[address] = lot_data
    with open("data/cached_lots.json", "w") as f:
        json.dump(cache, f, indent=2)


# ── Endpoints ───────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Compliance Checker API running"}


@app.post("/site")
def get_site(req: SiteRequest):
    """
    Given an address, return the lot polygon and zone.
    Checks cache first, fetches from NSW APIs if not cached.
    """
    try:
        cached = get_cached_lot(req.address)
        if cached:
            return {
                "address":  req.address,
                "lat":      cached["lat"],
                "lon":      cached["lon"],
                "zone":     cached["zone"],
                "polygon":  cached["polygon"],
                "cached":   True
            }

        print(f"Fetching from NSW APIs: {req.address}")
        lat, lon = geocode(req.address)
        time.sleep(0.5)
        polygon = get_lot_polygon(lat, lon)
        zone = get_zone(lat, lon, polygon=polygon)

        lot_data = {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon}
        save_to_cache(req.address, lot_data)

        return {
            "address":  req.address,
            "lat":      lat,
            "lon":      lon,
            "zone":     zone,
            "polygon":  polygon,
            "cached":   False
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/envelope")
def get_envelope(req: EnvelopeRequest):
    """
    Given an address, return the buildable envelope polygon
    computed from the lot polygon and DCP rules.
    """
    try:
        cached = get_cached_lot(req.address)
        if cached:
            lat     = cached["lat"]
            lon     = cached["lon"]
            zone    = cached["zone"]
            polygon = cached["polygon"]
        else:
            print(f"Fetching from NSW APIs: {req.address}")
            lat, lon = geocode(req.address)
            time.sleep(0.5)
            polygon = get_lot_polygon(lat, lon)
            zone = get_zone(lat, lon, polygon=polygon)
            lot_data = {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon}
            save_to_cache(req.address, lot_data)

        # Compute envelope
        envelope = compute_envelope(polygon, RULES, lat, lon)

        # Parameters to include in citations
        applied_params = {
            "front_setback", "rear_setback", "rear_setback_upper",
            "side_setback_ground", "side_setback_upper",
            "max_height", "max_storeys", "height_plane",
            "landscaped_area_pct", "private_open_space",
            "private_open_space_min_dimension"
        }

        # Filter rules: only relevant parameters, dwelling houses, standard lots
        applied_rules = [
            r for r in RULES
            if r.get("parameter") in applied_params
            and r.get("dwelling_type") in ("dwelling_house", "all")
            and r.get("lot_type") in ("single_frontage", "all", "not_specified")
        ]

        # Format citations with PDF links for the web app
        citations = []
        for r in applied_rules:
            citations.append({
                "parameter": r["parameter"],
                "value": r["value"],
                "unit": r["unit"],
                "operator": r["operator"],
                "clause": r.get("source_clause", ""),
                "page": r.get("source_page", 0),
                "text": r.get("source_text", ""),
                "conditions": r.get("conditions", []),
                "exceptions": r.get("exceptions", []),
                "pdf_link": f"/docs/canada_bay_dcp_part_e.pdf#page={r.get('source_page', 1)}"
            })

        return {
            "address":       req.address,
            "zone":          zone,
            "lot_polygon":   polygon,
            "envelope":      envelope,
            "rules_applied": citations
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))