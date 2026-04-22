import os

# Must be set before numpy/shapely are imported — fixes OpenBLAS memory
# allocation failures on Windows (manifests as 500 errors on every request).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import json
import sys
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR  = BASE_DIR.parent / "data"

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from nsw_apis import geocode, get_lot_polygon, get_zone, get_lga, get_fsr_from_map
from compute_envelope import compute_envelope
from check_lmr import check_lmr_eligibility

app = FastAPI(title="Automated Compliance Checker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load rules once at startup ───────────────────────────────

# Canada Bay (confidence-filtered)
with open(DATA_DIR / "rules_r2_canada_bay.json") as f:
    ALL_RULES = json.load(f)
RULES = [r for r in ALL_RULES if r.get("confidence", 0) >= 0.8]
print(f"Loaded {len(RULES)} Canada Bay rules")

# Housing SEPP Ch6 (LMR)
with open(DATA_DIR / "rules_housing_sepp_ch6.json") as f:
    SEPP_RULES = json.load(f)
print(f"Loaded {len(SEPP_RULES)} SEPP Ch6 rules")

# Inner West Council — R2 Low Density Residential suburbs only
# (former Leichhardt LGA is zoned R1 General Residential — outside R2 scope)
with open(DATA_DIR / "rules_inner_west_marrickville.json", encoding="utf-8") as f:
    IW_MARRICKVILLE = json.load(f)
with open(DATA_DIR / "rules_inner_west_ashfield.json", encoding="utf-8") as f:
    IW_ASHFIELD = json.load(f)

# Inner West LEP 2022 — merged into each R2 LGA rule set
with open(DATA_DIR / "rules_inner_west_lep.json", encoding="utf-8") as f:
    IW_LEP = json.load(f)

# LEP first so it takes precedence over DCP in first-match rule lookups
IW_MARRICKVILLE = IW_LEP + IW_MARRICKVILLE
IW_ASHFIELD     = IW_LEP + IW_ASHFIELD

print(f"Loaded Inner West R2 rules — Marrickville: {len(IW_MARRICKVILLE)}, "
      f"Ashfield: {len(IW_ASHFIELD)} (each includes {len(IW_LEP)} LEP rules)")

# Load DCP chunks (Part C + Part E) — Canada Bay only for now
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


# ── LGA routing ─────────────────────────────────────────────

# Ashfield DCP suburbs within Inner West — everything else uses Marrickville DCP
_ASHFIELD_SUBURBS = {"ASHFIELD", "CROYDON", "CROYDON PARK", "HABERFIELD", "DOBROYD POINT"}


def get_rules_for_lga(lga_name: str, address: str) -> tuple[list, str, str]:
    """
    Return (rules_list, lga_label, pdf_doc) using the authoritative LGA name
    from the NSW Planning API. Raises ValueError for unsupported councils.
    """
    lga_upper = lga_name.upper()
    if lga_upper == "INNER WEST":
        upper = address.upper()
        for suburb in _ASHFIELD_SUBURBS:
            if re.search(r'\b' + re.escape(suburb) + r'\b', upper):
                return (IW_ASHFIELD, "Inner West Council", "ashfield_dcp")
        return (IW_MARRICKVILLE, "Inner West Council", "marrickville_dcp")
    elif lga_upper == "CANADA BAY":
        return (RULES, "Canada Bay Council", "canada_bay_dcp_part_e")
    else:
        raise ValueError(
            f"Address is in {lga_name} — only Canada Bay and Inner West are currently supported."
        )


# ── Request models ──────────────────────────────────────────

class SiteRequest(BaseModel):
    address: str

class EnvelopeRequest(BaseModel):
    address: str


# ── Helper: check cache first ────────────────────────────────

CACHE_PATH = DATA_DIR / "cached_lots.json"


def get_cached_lot(address: str):
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
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


def _resolve_lot(address: str) -> tuple[float, float, str, dict, str, str, str]:
    """
    Return (lat, lon, zone, polygon, lga_name, lga_label, pdf_doc).
    Uses cache when available; fetches from NSW APIs otherwise.
    Raises ValueError for unsupported councils.
    FSR is fetched from the NSW Planning FSR Map and stored in cache under "fsr".
    """
    cached = get_cached_lot(address)
    if cached:
        lat, lon = cached["lat"], cached["lon"]
        zone, polygon = cached["zone"], cached["polygon"]
        dirty = False
        lga_name = cached.get("lga_name")
        if not lga_name:
            lga_name = get_lga(lat, lon)
            cached["lga_name"] = lga_name
            dirty = True
        if "fsr" not in cached:
            cached["fsr"] = get_fsr_from_map(lat, lon)
            dirty = True
        if dirty:
            save_to_cache(address, cached)
    else:
        print(f"Fetching from NSW APIs: {address}")
        lat, lon = geocode(address)
        time.sleep(0.5)
        lga_name = get_lga(lat, lon)
        polygon = get_lot_polygon(lat, lon)
        zone = get_zone(lat, lon, polygon=polygon)
        fsr = get_fsr_from_map(lat, lon)
        save_to_cache(address, {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon, "lga_name": lga_name, "fsr": fsr})

    _, lga_label, pdf_doc = get_rules_for_lga(lga_name, address)
    return lat, lon, zone, polygon, lga_name, lga_label, pdf_doc


@app.post("/site")
def get_site(req: SiteRequest):
    """
    Given an address, return the lot polygon, zone, and detected LGA.
    Checks cache first, fetches from NSW APIs if not cached.
    """
    try:
        cached = get_cached_lot(req.address)
        is_cached = cached is not None
        lat, lon, zone, polygon, _, lga_label, _ = _resolve_lot(req.address)

        return {
            "address":  req.address,
            "lat":      lat,
            "lon":      lon,
            "zone":     zone,
            "polygon":  polygon,
            "lga":      lga_label,
            "cached":   is_cached,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/envelope")
def get_envelope(req: EnvelopeRequest):
    """
    Given an address, return the buildable envelope polygon
    computed from the lot polygon and DCP rules for the correct LGA.
    """
    try:
        lat, lon, zone, polygon, lga_name, lga_label, pdf_doc = _resolve_lot(req.address)

        if zone not in ("R2",):
            zone_names = {
                "R1": "General Residential", "R3": "Medium Density Residential",
                "R4": "High Density Residential", "B4": "Mixed Use",
            }
            label = f"{zone} {zone_names.get(zone, '')}".strip()
            raise ValueError(
                f"This address is zoned {label} — only R2 Low Density Residential is currently supported."
            )

        rules, _, _ = get_rules_for_lga(lga_name, req.address)
        fsr_from_map = (get_cached_lot(req.address) or {}).get("fsr")

        # Pre-filter rules by zone + dwelling_type before passing to
        # compute_envelope so its first-match logic picks the correct rule.
        envelope_rules = [
            r for r in rules
            if r.get("zone") in (zone, "all_residential", "all")
            and r.get("dwelling_type") in ("dwelling_house", "all")
            and not r.get("superseded_by")
            and r.get("parameter") != "fsr"  # FSR comes from the map layer
        ]
        envelope = compute_envelope(polygon, envelope_rules, lat, lon)

        applied_params = {
            "front_setback", "rear_setback", "rear_setback_upper",
            "side_setback_ground", "side_setback_upper",
            "max_height", "max_storeys", "height_plane", "max_wall_height",
            "landscaped_area_pct", "site_coverage_pct",
            "private_open_space", "private_open_space_min_dimension",
            "building_separation",
            "parking_spaces_per_dwelling", "max_driveway_width",
            "front_fence_height_solid", "front_fence_height_open",
            "side_fence_height", "rear_fence_height"
        }

        applied_rules = [
            r for r in rules
            if r.get("parameter") in applied_params
            and r.get("zone") in (zone, "all_residential", "all")
            and r.get("dwelling_type") in ("dwelling_house", "all")
            and r.get("lot_type") in ("single_frontage", "all", "not_specified")
            and not r.get("superseded_by")
        ]

        # Group by parameter: one entry per parameter with a primary value
        # (unconditional rule) and a variants list for conditional rules.
        def _make_entry(r):
            return {
                "value":      r["value"],
                "unit":       r["unit"],
                "operator":   r["operator"],
                "clause":     r.get("source_clause", ""),
                "page":       r.get("source_page", 0),
                "text":       r.get("source_text", ""),
                "conditions": r.get("conditions", []),
                "exceptions": r.get("exceptions", []),
                "pdf_link":   f"/docs/{pdf_doc}.pdf#page={r.get('source_page', 1)}"
            }

        grouped: dict[str, dict] = {}
        for r in applied_rules:
            param = r["parameter"]
            entry = _make_entry(r)
            if param not in grouped:
                grouped[param] = {**entry, "parameter": param, "variants": []}
            else:
                # Primary = first unconditional rule; demote conditional ones to variants
                existing_is_conditional = bool(grouped[param]["conditions"])
                this_is_conditional = bool(entry["conditions"])
                if existing_is_conditional and not this_is_conditional:
                    # Promote this unconditional rule to primary, demote existing
                    old_primary = {k: grouped[param][k] for k in entry}
                    grouped[param].update({**entry, "parameter": param})
                    grouped[param]["variants"].append(old_primary)
                else:
                    grouped[param]["variants"].append(entry)

        citations = list(grouped.values())

        # Inject the single authoritative FSR from the LEP map layer
        if fsr_from_map is not None:
            citations.append({
                "parameter":  "fsr",
                "value":      fsr_from_map,
                "unit":       "ratio",
                "operator":   "max",
                "clause":     "Clause 4.4",
                "page":       0,
                "text":       f"Maximum floor space ratio: {fsr_from_map}:1 (from LEP FSR Map)",
                "conditions": [],
                "exceptions": [],
                "variants":   [],
                "pdf_link":   ""
            })

        return {
            "address":       req.address,
            "lga":           lga_label,
            "zone":          zone,
            "lot_polygon":   polygon,
            "envelope":      envelope,
            "rules_applied": citations
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
        lat, lon, zone, polygon, _, _, _ = _resolve_lot(req.address)
        lot_data = {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon}
        result = check_lmr_eligibility(req.address, lot_data, SEPP_RULES)
        return result

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
