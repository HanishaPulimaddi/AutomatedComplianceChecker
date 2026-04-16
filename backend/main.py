import json
import sys
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR  = BASE_DIR.parent / "data"

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from nsw_apis import geocode, get_lot_polygon, get_zone
from compute_envelope import compute_envelope_result
from check_lmr import check_lmr_eligibility, get_lmr_status
from merge_rules import get_applicable_rules as merge_applicable_rules

app = FastAPI(title="Automated Compliance Checker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load rules once at startup
with open(DATA_DIR / "rules_r2_canada_bay.json") as f:
    ALL_RULES = json.load(f)

RULES = [r for r in ALL_RULES if r.get("confidence", 0) >= 0.8]
print(f"Loaded {len(RULES)} rules at startup")

with open(DATA_DIR / "rules_housing_sepp_ch6.json") as f:
    SEPP_RULES = json.load(f)
print(f"Loaded {len(SEPP_RULES)} SEPP Ch6 rules at startup")

# Load DCP chunks (Part C + Part E)
def _load_jsonl(path: Path):
    chunks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks

CHUNKS_PART_C = _load_jsonl(DATA_DIR / "chunks_canada_bay_dcp_part_c.jsonl")
CHUNKS_PART_E = _load_jsonl(DATA_DIR / "chunks_canada_bay_dcp_part_e.jsonl")
ALL_CHUNKS = CHUNKS_PART_C + CHUNKS_PART_E
print(f"Loaded {len(CHUNKS_PART_C)} Part C chunks, {len(CHUNKS_PART_E)} Part E chunks")


# ── Request models ──────────────────────────────────────────

class SiteRequest(BaseModel):
    address: str

class EnvelopeRequest(BaseModel):
    address: str
    dwelling_type: str = "dwelling_house"
    lot_type: str = "single_frontage"
    housing_type: str | None = None


# ── Helper: check cache first ────────────────────────────────

CACHE_PATH = DATA_DIR / "cached_lots.json"


def get_cached_lot(address: str):
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        # Strip stray whitespace from keys written by earlier run_apis.py runs
        cache = {k.strip(): v for k, v in cache.items()}
        if address in cache:
            print(f"  Cache hit: {address}")
            return cache[address]
    except FileNotFoundError:
        pass
    return None


def save_to_cache(address: str, lot_data: dict):
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        cache = {k.strip(): v for k, v in cache.items()}
    except FileNotFoundError:
        cache = {}
    cache[address] = lot_data
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def _pdf_link_for_rule(rule: dict) -> str:
    source_doc = rule.get("source_document", "").lower()
    page = rule.get("source_page", 1)
    if "housing sepp" in source_doc or rule.get("source_type") == "sepp_manual":
        return f"/docs/housing_sepp_2021.pdf#page={page}"
    if "part c" in source_doc:
        return f"/docs/canada_bay_dcp_part_c.pdf#page={page}"
    return f"/docs/canada_bay_dcp_part_e.pdf#page={page}"


def _rule_citation(rule: dict) -> dict:
    return {
        "rule_id": rule.get("rule_id", ""),
        "parameter": rule.get("parameter", ""),
        "value": rule.get("value"),
        "unit": rule.get("unit", ""),
        "operator": rule.get("operator", ""),
        "clause": rule.get("source_clause", ""),
        "page": rule.get("source_page", 0),
        "text": rule.get("source_text", ""),
        "conditions": rule.get("conditions", []),
        "exceptions": rule.get("exceptions", []),
        "source_document": rule.get("source_document", ""),
        "source_type": rule.get("source_type", ""),
        "confidence": rule.get("confidence"),
        "verified": rule.get("verified", False),
        "pdf_link": _pdf_link_for_rule(rule),
    }


# ── Endpoints ───────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Compliance Checker API running"}


@app.get("/chunks")
def search_chunks(
    q: str = Query(default="", description="Keyword to search in chunk text"),
    part: str = Query(default="", description="Filter by part: 'c' or 'e'"),
    limit: int = Query(default=20, le=100),
):
    """Search DCP chunks by keyword and/or part (c/e)."""
    pool = ALL_CHUNKS
    if part.lower() == "c":
        pool = CHUNKS_PART_C
    elif part.lower() == "e":
        pool = CHUNKS_PART_E

    if q:
        q_lower = q.lower()
        pool = [ch for ch in pool if q_lower in ch["text"].lower()]

    return {"total": len(pool), "chunks": pool[:limit]}


@app.post("/site")
def get_site(req: SiteRequest):
    """
    Given an address, return the lot polygon and zone.
    Checks cache first, fetches from NSW APIs if not cached.
    """
    try:
        # Check cache first
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

        # Not in cache — fetch from NSW APIs
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
        # Get site data (from cache or NSW APIs)
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

        lmr_info = get_lmr_status(lat, lon)
        lmr_status = lmr_info.get("status")
        effective_lmr_status = lmr_status if req.housing_type else None

        merged = merge_applicable_rules(
            dcp_rules=RULES,
            sepp_rules=SEPP_RULES,
            zone=zone,
            dwelling_type=req.dwelling_type,
            lot_type=req.lot_type,
            lmr_status=effective_lmr_status,
            housing_type=req.housing_type,
        )
        effective_rules = merged["applied_rules"]
        source_breakdown = merged["source_summary"]
        source_breakdown.update({
            "lmr_status": lmr_status or "not in LMR area",
            "effective_lmr_status": effective_lmr_status or "not applied",
            "housing_type": req.housing_type,
            "sepp_overrides_applied": bool(effective_lmr_status and req.housing_type),
            "note": (
                "SEPP overrides require housing_type in the /envelope request"
                if lmr_status and not req.housing_type
                else ""
            ),
        })

        envelope_result = compute_envelope_result(polygon, effective_rules, lat, lon)
        envelope = envelope_result["envelope"]
        development_controls = envelope_result["development_controls"]

        # Find which rules were actually applied — filter by zone so R2-only
        # rules don't appear for R3 lots (or vice versa).
        applied_params = {
            "front_setback", "rear_setback", "rear_setback_upper",
            "side_setback_ground", "side_setback_upper",
            "max_height", "max_storeys", "height_plane",
            "landscaped_area_pct", "private_open_space",
            "private_open_space_min_dimension", "fsr",
            "min_lot_size", "min_lot_width",
            "min_parking_per_dwelling",
            "subdivision_min_lot_size", "subdivision_min_lot_width"
        }

        applied_rules = [
            r for r in effective_rules
            if r.get("parameter") in applied_params
            and r.get("zone") in (zone, "all_residential")
            and r.get("dwelling_type") in (req.dwelling_type, req.housing_type, "all")
            and r.get("lot_type") in (req.lot_type, "all", "not_specified")
        ]

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
                "source_document": r.get("source_document", ""),
                "source_type": r.get("source_type", ""),
                "confidence": r.get("confidence"),
                "verified": r.get("verified", False),
                "pdf_link": _pdf_link_for_rule(r)
            })

        return {
            "address":       req.address,
            "lga":           cached.get("lga") if cached else None,
            "suburb":        cached.get("suburb") if cached else None,
            "zone":          zone,
            "lot_polygon":   polygon,
            "envelope":      envelope,
            "development_controls": development_controls,
            "rules_applied": citations,
            "lmr_info":      lmr_info,
            "rule_source_breakdown": source_breakdown,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/lmr")
def get_lmr(req: SiteRequest):
    """
    Run the Housing SEPP 2021 Ch6 LMR pre-flight check for an address.

    Returns zone eligibility and per-housing-type dimensional checks
    (min lot size, min lot width) using the cached lot polygon.

    Scope: all_lmr rules only. Inner/outer area rules (s180) require a
    walking-distance-to-station check not yet wired into this endpoint.
    """
    try:
        lot_data = get_cached_lot(req.address)
        if not lot_data:
            # Fetch from NSW APIs if not cached
            print(f"Fetching from NSW APIs: {req.address}")
            lat, lon = geocode(req.address)
            time.sleep(0.5)
            polygon = get_lot_polygon(lat, lon)
            zone = get_zone(lat, lon, polygon=polygon)
            lot_data = {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon}
            save_to_cache(req.address, lot_data)

        result = check_lmr_eligibility(req.address, lot_data, SEPP_RULES)
        return result

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
