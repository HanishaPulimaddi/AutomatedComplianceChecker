import os

# Must be set before numpy/shapely are imported — fixes OpenBLAS memory
# allocation failures on Windows (manifests as 500 errors on every request).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR  = PROJECT_ROOT / "data"
DOCS_DIR  = PROJECT_ROOT / "docs"
GRASSHOPPER_PLUGIN_PATH = PROJECT_ROOT / "RhinoPlugInV1.gh"

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nsw_apis import (
    geocode, get_lot_polygon, get_zone, get_lga,
    get_fsr_from_map, get_hob_from_map, get_min_lot_size_from_map, get_heritage_from_map,
    get_roads_near_point, get_address_suggestions,
)
from compute_envelope import compute_envelope
from check_lmr import check_lmr_eligibility

app = FastAPI(title="Automated Compliance Checker", version="1.0.0")

# The two localhost origins cover local dev only. Once the frontend is
# deployed to a real domain (e.g. Vercel), the browser will silently block
# it from reading /envelope, /cdc-eligibility and /address-suggestions
# responses unless that exact domain is in this list too — confirmed live:
# a request from "https://myapp.vercel.app" got no Access-Control-Allow-Origin
# header back at all, which the browser treats as a hard block. Set
# FRONTEND_ORIGIN to the real deployed frontend URL so this isn't silently
# broken in production; comma-separate if there's more than one (e.g. a
# preview + production Vercel URL).
_extra_origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", *_extra_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every citation's "pdf_link" field points at /docs/<file>.pdf — this was
# never actually backed by a real route, so every one of those links has
# been 404ing since the feature was built. Mounting the docs/ folder here
# is what makes them real.
app.mount("/docs", StaticFiles(directory=DOCS_DIR), name="docs")

# ── Load rules once at startup ───────────────────────────────

# Canada Bay R2 (confidence-filtered)
with open(DATA_DIR / "rules_r2_canada_bay.json") as f:
    ALL_RULES = json.load(f)
RULES = [r for r in ALL_RULES if r.get("confidence", 0) >= 0.8]
print(f"Loaded {len(RULES)} Canada Bay R2 rules")

# Canada Bay R3 (multi-dwelling housing, terraces, manor houses, residential flat buildings)
with open(DATA_DIR / "rules_r3_canada_bay_pipeline.json", encoding="utf-8") as f:
    ALL_RULES_R3 = json.load(f)
RULES_R3 = [r for r in ALL_RULES_R3 if r.get("confidence", 0) >= 0.8]
print(f"Loaded {len(RULES_R3)} Canada Bay R3 rules")

# Canada Bay Part G — Five Dock Town Centre carve-out (R3 land excluded from
# Part F, bordering Barnstaple Road/Waterview Street/Second Avenue). Setback
# and height there are set by colour-coded DCP figures (Figs G3.46/47/49/52),
# not text values — no per-lot number is asserted for those; only the
# genuinely address-independent standards (private open space, upper-storey
# setback default, floor heights) are loaded as real citations.
with open(DATA_DIR / "rules_canada_bay_part_g_five_dock.json", encoding="utf-8") as f:
    RULES_PART_G_FIVE_DOCK = json.load(f)
print(f"Loaded {len(RULES_PART_G_FIVE_DOCK)} Canada Bay Part G (Five Dock Town Centre) rules")

# Housing SEPP Ch6 (LMR)
with open(DATA_DIR / "rules_housing_sepp_ch6.json") as f:
    SEPP_RULES = json.load(f)
print(f"Loaded {len(SEPP_RULES)} SEPP Ch6 rules")

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

# ── source_document -> actual PDF filename ───────────────────
# Every rule carries its own source_document string; a citation's "verify
# against source" link must resolve THAT document, not a single filename
# assumed for the whole LGA/zone response (wrong for any rule sourced from
# a different part than the "primary" one — e.g. Part B General Controls
# rules mixed into an R2/R3/R4 response whose primary document is Part E/F).
_SOURCE_DOC_TO_PDF = {
    "Canada Bay DCP Part E": "canada-bay-dcp-part-e-dwelling-houses",
    "Canada Bay DCP Part E (Single and Semi-Detached Dwellings)": "canada-bay-dcp-part-e-dwelling-houses",
    "Canada Bay DCP Part B (General Controls)": "canada-bay-dcp-part-b-general-controls",
    "Canada Bay DCP Part F (Multi-dwelling Housing, Terraces, Manor Houses, Residential Flat Buildings)": "canada-bay-dcp-part-f-multi-dwelling-housing",
    "Housing SEPP 2021 Chapter 6": "nsw-housing-sepp-2021",
    "Housing SEPP 2021": "nsw-housing-sepp-2021",
    "Canada Bay Local Environmental Plan 2013": "canada-bay-lep-2013",
    "Apartment Design Guide Part 3 (Siting the development)": "nsw-adg-part-3-siting-the-development",
    "Apartment Design Guide Part 4 (Designing the building)": "nsw-adg-part-4-designing-the-building",
    "Canada Bay DCP Part C (Heritage)": "canada-bay-dcp-part-c-heritage",
    "Codes SEPP 2008 - Part 3 (Housing Code)": "nsw-codes-sepp-2008",
    "Codes SEPP 2008 - Part 3B (Low Rise Housing Diversity Code)": "nsw-codes-sepp-2008",
    "Canada Bay DCP Part D (Boarding Houses)": "canada-bay-dcp-part-d-boarding-houses",
    "Canada Bay DCP Part J (Child Care Centres)": "canada-bay-dcp-part-j-child-care-centres",
}


def _pdf_link_for(source_document: str, page: int) -> str:
    filename = _SOURCE_DOC_TO_PDF.get(source_document)
    if not filename:
        return ""
    from urllib.parse import quote
    return f"/docs/{quote(filename)}.pdf#page={page or 1}"


def _make_rule_entry(r: dict) -> dict:
    source_doc = r.get("source_document", "")
    return {
        "value":           r["value"],
        "unit":            r["unit"],
        "operator":        r["operator"],
        "clause":          r.get("source_clause", ""),
        "page":            r.get("source_page", 0),
        "text":            r.get("source_text", ""),
        "conditions":      r.get("conditions", []),
        "exceptions":      r.get("exceptions", []),
        "dwelling_type":   r.get("dwelling_type", "all"),
        "source_document": source_doc,
        "source_type":     r.get("source_type", "unknown"),
        "confidence":      r.get("confidence"),
        "verified":        r.get("verified", False),
        "pdf_link":        _pdf_link_for(source_doc, r.get("source_page", 1)),
    }


# How long a cached live-map lookup (FSR/height/lot size/heritage) is trusted
# before being re-fetched. These reflect council LEP amendments, which happen
# on the order of months, not days — 30 days balances staleness risk against
# hammering the NSW API on every repeat lookup of the same address.
MAP_DATA_TTL_S = 30 * 24 * 60 * 60

# Version/date stamp printed on each source PDF at extraction time — surfaced
# in API responses so a user can see exactly which edition of the DCP this
# reflects and check council's site for anything newer. Not auto-detected;
# these are the versions read directly off each PDF's footer.
DCP_SOURCE_VERSIONS = {
    "Canada Bay DCP Part E": {"version": 3, "date": "2025-06-26"},
    "Canada Bay DCP Part E (Single and Semi-Detached Dwellings)": {"version": 3, "date": "2025-06-26"},
    "Canada Bay DCP Part B (General Controls)": {"version": 9, "date": "2025-06-26"},
    "Canada Bay DCP Part F (Multi-dwelling Housing, Terraces, Manor Houses, Residential Flat Buildings)": {"version": 3, "date": "2025-06-26"},
    "Canada Bay DCP Part C (Heritage)": {"version": 1, "date": "2025-06-26"},
    "Codes SEPP 2008 - Part 3 (Housing Code)": {"version": None, "date": "2026-03-13"},
    "Codes SEPP 2008 - Part 3B (Low Rise Housing Diversity Code)": {"version": None, "date": "2026-03-13"},
    "Canada Bay DCP Part D (Boarding Houses)": {"version": 3, "date": "2025-06-26"},
    "Canada Bay DCP Part J (Child Care Centres)": {"version": 3, "date": "2025-06-26"},
}

# Part F (F1, "Land to which Part F applies") explicitly excludes R3 land in
# the Five Dock Town Centre with a boundary to any of these three roads —
# that pocket is governed by a separate Part G, which we don't have. Applying
# Part F there anyway would be confidently wrong, not just missing data, so
# this is checked and blocked rather than left silent. A false positive here
# just means "we don't have this" instead of a wrong number — an acceptable
# trade, unlike the corner-lot geometry check above.
_FIVE_DOCK_TOWN_CENTRE_BOUNDARY_ROADS = {"Barnstaple Road", "Waterview Street", "Second Avenue"}


def _in_five_dock_town_centre_carveout(address: str, lat: float, lon: float) -> bool:
    upper = address.upper()
    if "FIVE DOCK" not in upper:
        return False
    for road in _FIVE_DOCK_TOWN_CENTRE_BOUNDARY_ROADS:
        if road.upper() in upper:
            return True
    try:
        nearby = get_roads_near_point(lat, lon, radius_m=30)
    except Exception:
        return False
    return any(r in _FIVE_DOCK_TOWN_CENTRE_BOUNDARY_ROADS for r in nearby)


# ── LGA routing ─────────────────────────────────────────────

def get_rules_for_lga(lga_name: str, address: str, zone: str | None = None) -> tuple[list, str, str]:
    """
    Return (rules_list, lga_label, pdf_doc) using the authoritative LGA name
    from the NSW Planning API. Raises ValueError for unsupported councils.

    zone selects between Canada Bay's R2 (dwelling houses) and R3/R4
    (multi-dwelling housing / terraces / manor houses / residential flat
    buildings) rule sets. Part F's own applicability clause (F1) is
    dwelling-type gated, not zone-code gated — residential flat buildings
    are R4's characteristic building type, so R4 uses the same Part F
    ruleset as R3 (height is then overridden per-lot from the live LEP
    Height of Building map in /envelope, since Part F's own height table
    only states a fixed storey cap for the low LEP-height case).
    """
    lga_upper = lga_name.upper()
    if lga_upper == "CANADA BAY":
        if zone in ("R3", "R4"):
            return (RULES_R3, "Canada Bay Council", "canada_bay_dcp_part_f")
        return (RULES, "Canada Bay Council", "canada_bay_dcp_part_e")
    else:
        raise ValueError(
            f"Address is in {lga_name} — only Canada Bay is currently supported."
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


@app.get("/grasshopper-plugin")
def download_grasshopper_plugin():
    """Serves the actual .gh file as a real download — the frontend button
    previously pointed nowhere (href="#" with preventDefault())."""
    if not GRASSHOPPER_PLUGIN_PATH.exists():
        raise HTTPException(status_code=404, detail="Grasshopper plugin file not found on the server.")
    return FileResponse(
        GRASSHOPPER_PLUGIN_PATH,
        media_type="application/octet-stream",
        filename="CanadaBayComplianceChecker.gh",
    )


@app.get("/address-suggestions")
def address_suggestions(q: str = Query(default="", description="Partial address text typed so far")):
    """
    Autocomplete-as-you-type: most users don't type a full, correctly
    formatted address, so this returns ranked suggestions from the same
    geocoder /envelope uses, biased toward Canada Bay. The frontend calls
    this on every keystroke (debounced) to show a dropdown.
    """
    try:
        return {"suggestions": get_address_suggestions(q)}
    except Exception:
        # Suggestions are a convenience, not core functionality — a flaky
        # network call here should never surface as an error to the user,
        # just fall back to an empty dropdown.
        return {"suggestions": []}


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

    Four numeric/overlay LEP controls are always fetched live from the NSW
    Planning spatial map layers rather than trusted from DCP text, because
    all four vary by precinct rather than being one flat LGA-wide number:
      - fsr  (Floor Space Ratio map)
      - hob  (Height of Building map — R4 residential flat buildings alone
              have been seen ranging 25m-82m within one precinct; even
              R2/R3, where the DCP's flat 8.5m usually matches, can differ
              on heritage/town-centre-fringe lots)
      - min_lot_size (Lot Size map — Canada Bay's DCP doesn't state this
              at all, the map is the only source)
      - heritage (Heritage map — categorical overlay, not a numeric
              substitution; flags when Part C Heritage controls may apply)

    These four are re-fetched if the cached copy is older than MAP_DATA_TTL_S —
    a permanently-cached lookup would keep serving pre-amendment values forever
    even after council updates a map, defeating the point of going live in the
    first place. Cached forever otherwise (lat/lon/zone/polygon don't change).
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
        map_age_s = time.time() - cached.get("map_data_fetched_at", 0)
        map_data_stale = map_age_s > MAP_DATA_TTL_S
        if "fsr" not in cached or map_data_stale:
            cached["fsr"] = get_fsr_from_map(lat, lon)
            dirty = True
        if "hob" not in cached or map_data_stale:
            cached["hob"] = get_hob_from_map(lat, lon)
            dirty = True
        if "min_lot_size" not in cached or map_data_stale:
            cached["min_lot_size"] = get_min_lot_size_from_map(lat, lon)
            dirty = True
        if "heritage" not in cached or map_data_stale:
            cached["heritage"] = get_heritage_from_map(lat, lon)
            dirty = True
        if dirty:
            cached["map_data_fetched_at"] = time.time()
            save_to_cache(address, cached)
    else:
        print(f"Fetching from NSW APIs: {address}")
        lat, lon = geocode(address)
        time.sleep(0.5)
        lga_name = get_lga(lat, lon)
        polygon = get_lot_polygon(lat, lon)
        zone = get_zone(lat, lon, polygon=polygon)
        fsr = get_fsr_from_map(lat, lon)
        hob = get_hob_from_map(lat, lon)
        min_lot_size = get_min_lot_size_from_map(lat, lon)
        heritage = get_heritage_from_map(lat, lon)
        save_to_cache(address, {
            "lat": lat, "lon": lon, "zone": zone, "polygon": polygon, "lga_name": lga_name,
            "fsr": fsr, "hob": hob, "min_lot_size": min_lot_size, "heritage": heritage,
            "map_data_fetched_at": time.time(),
        })

    _, lga_label, pdf_doc = get_rules_for_lga(lga_name, address, zone)
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

        supported_zones = ("R1", "R2", "R3", "R4") if lga_name.upper() == "CANADA BAY" else ("R2",)
        if zone not in supported_zones:
            zone_names = {
                "R1": "General Residential", "R3": "Medium Density Residential",
                "R4": "High Density Residential",
                "MU1": "Mixed Use", "E1": "Local Centre", "E3": "Productivity Support",
                "SP2": "Infrastructure",
            }
            label = f"{zone} {zone_names.get(zone, '')}".strip()
            supported_label = ("R1 General, R2 Low Density, R3 Medium Density or R4 High Density"
                                if "R4" in supported_zones else "R2 Low Density")
            raise ValueError(
                f"This address is zoned {label} — only {supported_label} Residential is currently supported."
            )

        # Part E's own applicability clause (E1, "Land to which Part E
        # applies") is dwelling-type gated ("single dwellings, semi-detached
        # dwellings, dual occupancies and secondary dwellings"), not
        # zone-code gated — it says nothing about R1 vs R2. R1 (General
        # Residential) permits the same dwelling house use as R2, so R1
        # addresses use the same Part E ruleset as their primary/default
        # scenario. Rules were tagged "R2" at extraction time (since that's
        # what the source addresses happened to be), not because Part E is
        # genuinely R2-exclusive — so R1 requests are matched against
        # R2-tagged rules too.
        #
        # BUT R1's own LEP land use table (checked directly against the
        # council's permitted-use list) shows it ALSO permits multi dwelling
        # housing, residential flat buildings and shop-top housing — the
        # same building types R3/R4 use Part F for. Unlike every other zone,
        # the zone code alone doesn't tell us which the applicant means, so
        # R1 gets a second, clearly-labelled "alternate_development_scenario"
        # computed under Part F further down, rather than silently picking
        # one and hiding the other.
        zone_for_rule_matching = "R2" if zone == "R1" else zone

        is_five_dock_carveout = (
            zone == "R3" and lga_name.upper() == "CANADA BAY"
            and _in_five_dock_town_centre_carveout(req.address, lat, lon)
        )

        if is_five_dock_carveout:
            rules = RULES_PART_G_FIVE_DOCK
        else:
            rules, _, _ = get_rules_for_lga(lga_name, req.address, zone)
        cached_lot      = get_cached_lot(req.address) or {}
        fsr_from_map    = cached_lot.get("fsr")
        hob_from_map    = cached_lot.get("hob")
        lot_size_from_map = cached_lot.get("min_lot_size")
        heritage_info   = cached_lot.get("heritage")

        def _build_scenario(rules_source: list, matching_zone: str, height_open_ended: bool, skip_envelope: bool):
            """
            Filter `rules_source` down to citations + a computed envelope for
            one specific ruleset. Factored out so R1 can run this twice —
            once against Part E (dwelling house), once against Part F
            (multi-dwelling/apartment) — without duplicating this whole block.

            height_open_ended mirrors the R4-only "Part F's storey cap only
            holds at the 8.5m LEP case" logic, but keyed to whether Part F is
            actually the ruleset in play here, not the literal zone code —
            it fires for R4 always, and for R1's Part F alternate scenario
            too, since the same DCP caveat applies whenever Part F rules and
            a >8.5m live height limit are used together.
            """
            excluded = {"fsr"}
            if not heritage_info:
                excluded.add("heritage_cut_fill_max")
                excluded.add("heritage_pavilion_addition_separation_min")
            if hob_from_map is not None:
                excluded.add("max_height")
                if height_open_ended and hob_from_map > 8.5:
                    excluded.add("max_storeys")

            zone_rules_local = [
                r for r in rules_source
                if r.get("zone") in (matching_zone, "all_residential", "all")
                and not r.get("superseded_by")
                and r.get("parameter") not in excluded
            ]
            env_rules = [r for r in zone_rules_local if r.get("dwelling_type") in ("dwelling_house", "all")]
            params_with_default = {r["parameter"] for r in env_rules}
            env_rules += [
                r for r in zone_rules_local
                if r.get("dwelling_type") not in ("dwelling_house", "all")
                and r["parameter"] not in params_with_default
            ]

            env = None
            warnings_ = []
            if not skip_envelope:
                env, warnings_ = compute_envelope(polygon, env_rules, lat, lon)

            return rules_source, matching_zone, excluded, env, warnings_

        # Pre-filter rules by zone before passing to compute_envelope so its
        # first-match logic picks the correct rule. Geometry defaults to the
        # dwelling_house/all case (the only checking mode the API supports);
        # a parameter with no dwelling_house/all rule falls back to any other
        # dwelling_type rather than silently having no value at all.
        # FSR always comes from the map layer. max_height comes from the map
        # layer too when available (DCP's flat figure doesn't hold for R4,
        # and can miss precinct exceptions even in R2/R3) — only fall back
        # to the DCP-stated value when the map has no coverage for this lot.
        rules, zone_for_rule_matching, excluded_params, envelope, fallback_warnings = _build_scenario(
            rules, zone_for_rule_matching, height_open_ended=(zone == "R4"), skip_envelope=is_five_dock_carveout,
        )

        applied_params = {
            "front_setback", "rear_setback", "rear_setback_upper",
            "side_setback_ground", "side_setback_upper", "balcony_rear_setback",
            "max_height", "max_storeys", "height_plane", "max_wall_height",
            "landscaped_area_pct", "site_coverage_pct",
            "private_open_space", "private_open_space_min_dimension",
            "building_separation",
            "parking_spaces_per_dwelling", "max_driveway_width",
            "front_fence_height_solid", "front_fence_height_open",
            "side_fence_height", "rear_fence_height",
            "adaptable_housing_pct", "min_habitable_floor_level",
            "topography_cut_fill_max",
            "waste_bin_walking_distance_max", "waste_vehicle_clearance_height",
            "waste_vehicle_access_width_min",
            "protected_tree_min_height", "protected_tree_min_trunk_diameter",
            "protected_tree_min_canopy_spread", "tree_setback_from_dwelling",
            "tree_replacement_ratio", "solar_access_hours_min",
            "foreshore_public_access_width", "seawall_height_max",
            "pool_coping_height_max", "fence_exclusion_zone_from_water",
            "retaining_wall_height_max", "ramp_crest_level_max_drop",
            "driveway_landscape_strip_width_min", "outbuilding_floor_area_max",
            "flood_parking_level_offset", "flood_tailwater_level",
            "concessional_development_addition_max", "garage_frontage_occupancy_max",
            "garage_structure_width_max", "waste_bin_carting_route_width_min",
            "exempt_tree_species_height_max", "tree_canopy_target_pct",
            "hardstand_front_setback_min", "secondary_facade_offset", "secondary_facade_max_width_pct",
            "dormer_height_max", "dwelling_massing_offset_max",
            "landscape_planting_min_mature_height", "pathway_boundary_setback",
            "fence_boundary_setback", "satellite_dish_height_max",
            "height_plane_ground_tolerance_max", "deck_patio_height_max",
            "upper_level_setback_above_four_storeys", "facade_articulation_zone_depth",
            "min_floor_to_ceiling_height", "five_dock_town_centre_height_tier_reference",
            "dwelling_mix_studio_1bed_min_pct", "dwelling_mix_3bed_plus_min_pct",
            "design_excellence_height_trigger", "competitive_design_process_height_trigger",
            "acid_sulfate_soils_class_reference", "affordable_housing_levy_pct",
            "communal_open_space_min_pct", "communal_open_space_solar_access_pct",
            "communal_open_space_min_area_per_dwelling", "communal_open_space_min_dimension",
            "deep_soil_zone_min_pct", "deep_soil_zone_min_dimension",
            "adg_boundary_separation_habitable", "adg_boundary_separation_non_habitable",
            "apartment_solar_access_pct_min", "apartment_no_solar_access_pct_max",
            "natural_cross_ventilation_pct_min", "cross_through_apartment_depth_max",
            "apartment_min_internal_area", "habitable_room_window_glass_area_min_pct",
            "open_plan_habitable_room_depth_max", "bedroom_min_area", "bedroom_min_dimension",
            "living_room_min_width", "cross_through_apartment_width_min",
            "adg_balcony_min_area", "adg_balcony_min_depth",
            "adg_ground_floor_pos_min_area", "adg_ground_floor_pos_min_depth",
            "apartment_storage_min_volume",
            "secondary_dwelling_max_floor_area", "secondary_dwelling_min_site_area",
            "secondary_dwelling_complying_dev_min_lot_size", "adg_prevails_over_dcp_matters",
            "tod_max_height_floor_rfb", "tod_max_height_floor_ilu_shoptop", "tod_max_fsr_floor",
            "tod_min_lot_width", "tod_affordable_housing_pct",
            "tod_affordable_housing_parking_1bed", "tod_affordable_housing_parking_2bed",
            "tod_affordable_housing_parking_3bed_plus",
            "heritage_cut_fill_max", "heritage_pavilion_addition_separation_min",
            "boarding_room_min_area_single", "boarding_room_min_area_double", "boarding_room_max_area",
            "boarding_room_max_occupancy", "boarding_house_kitchen_min_area",
            "boarding_house_laundry_circulation_min_width", "boarding_house_social_impact_assessment_trigger",
            "boarding_house_bicycle_parking_per_lodger",
            "childcare_parking_spaces_per_licensed_places", "childcare_max_sign_area", "childcare_setback_note",
            # Found by an audit for extracted-but-never-surfaced parameters —
            # these were correctly extracted from the DCP at some point but
            # never added to this whitelist, so they never appeared in any
            # citation despite being real, stated standards.
            "primary_facade_width_pct", "max_driveway_width_pct", "privacy_sill_height",
            "balcony_side_setback", "landscaping_strip_width", "landscaped_area_front_pct",
            "landscaped_area_rear_pct", "pool_coping_boundary_setback_min",
            "min_dwelling_width", "basement_setback", "outbuilding_setback",
            "min_parking_space_length", "min_parking_space_width",
            "residential_exclusion_buffer_from_road", "roof_pitch_min", "roof_pitch_max",
        }

        # Group by parameter: one entry per parameter with a primary value
        # (unconditional rule) and a variants list for conditional rules.
        def _is_conditional(entry: dict) -> bool:
            # A rule scoped to a specific dwelling type (not the general
            # dwelling_house/all case) is conditional even if conditions[]
            # is empty — the dwelling_type itself is the condition.
            return bool(entry["conditions"]) or entry["dwelling_type"] not in ("all", "dwelling_house")

        # LEP cl 4.6 lets Council approve development that exceeds ANY LEP
        # standard (including FSR and height) if the applicant justifies it
        # — a discretionary variation pathway, not a fixed number, so it's
        # not modelled as its own rule. Repeated here as a standing caveat
        # on every LEP-map-sourced figure so it isn't presented as
        # absolute.
        _CL_4_6_NOTE = ("Stated as the LEP standard. Clause 4.6 (Exceptions to development "
                         "standards) allows Council to approve development that exceeds this "
                         "figure if justified — this is not itself a fixed number, so it isn't "
                         "modelled as a rule, but it means this value is a standard, not an "
                         "absolute ceiling.")

        def _build_citations(rules_source: list, matching_zone: str, excluded: set) -> list:
            """
            Group `rules_source` into one citation per parameter, then inject
            the live-map FSR/height/lot-size figures (same for every scenario
            at this address, since they come from the lot's location, not
            the ruleset). Factored out so R1's alternate Part F scenario gets
            its own independently-built citations list, not a reused one.
            """
            applied_rules_local = [
                r for r in rules_source
                if r.get("parameter") in applied_params
                and r.get("parameter") not in excluded
                and r.get("zone") in (matching_zone, "all_residential", "all")
                and r.get("lot_type") in ("single_frontage", "all", "not_specified")
                and not r.get("superseded_by")
            ]

            grouped_local: dict[str, dict] = {}
            for r in applied_rules_local:
                param = r["parameter"]
                entry = _make_rule_entry(r)
                if param not in grouped_local:
                    grouped_local[param] = {**entry, "parameter": param, "variants": []}
                else:
                    existing_is_conditional = _is_conditional(grouped_local[param])
                    this_is_conditional = _is_conditional(entry)
                    if existing_is_conditional and not this_is_conditional:
                        old_primary = {k: grouped_local[param][k] for k in entry}
                        grouped_local[param].update({**entry, "parameter": param})
                        grouped_local[param]["variants"].append(old_primary)
                    else:
                        grouped_local[param]["variants"].append(entry)

            citations_local = list(grouped_local.values())

            if fsr_from_map is not None:
                citations_local.append({
                    "parameter": "fsr", "value": fsr_from_map, "unit": "ratio", "operator": "max",
                    "clause": "Clause 4.4", "page": 0,
                    "text": f"Maximum floor space ratio: {fsr_from_map}:1 (from LEP FSR Map)",
                    "conditions": [_CL_4_6_NOTE], "exceptions": [], "variants": [], "pdf_link": "",
                })
            if hob_from_map is not None:
                citations_local.append({
                    "parameter": "max_height", "value": hob_from_map, "unit": "m", "operator": "max",
                    "clause": "Clause 4.3", "page": 0,
                    "text": f"Maximum building height: {hob_from_map}m (from LEP Height of Building Map)",
                    "conditions": [_CL_4_6_NOTE], "exceptions": [], "variants": [], "pdf_link": "",
                })
            if lot_size_from_map is not None:
                citations_local.append({
                    "parameter": "min_lot_size", "value": lot_size_from_map, "unit": "m2", "operator": "min",
                    "clause": "Clause 4.1", "page": 0,
                    "text": f"Minimum lot size: {lot_size_from_map}m2 (from LEP Lot Size Map)",
                    "conditions": [], "exceptions": [], "variants": [], "pdf_link": "",
                })
            if "max_storeys" in excluded:
                citations_local.append({
                    "parameter": "max_storeys", "value": None, "unit": "storeys", "operator": "not_fixed",
                    "clause": "F4.4 C1", "page": 0,
                    "text": (f"Not fixed by the DCP — Part F caps storeys at 2 only where the LEP "
                             f"height limit is 8.5m; this lot's LEP limit is {hob_from_map}m, so storey "
                             f"count is governed by the height limit above, not a fixed count."),
                    "conditions": [], "exceptions": [], "variants": [], "pdf_link": "",
                })
            return citations_local

        citations = _build_citations(rules, zone_for_rule_matching, excluded_params)

        # R1 alone permits both a dwelling house (Part E, computed above as
        # the primary/default answer) AND multi-dwelling housing/residential
        # flat buildings (Part F) on the same zone code — the LEP land use
        # table lists both as "permitted with consent" for R1, and nothing
        # about the address tells us which one this applicant means. Compute
        # a second, clearly-labelled scenario under Part F rather than
        # silently guessing one and hiding the other.
        alternate_scenario = None
        if zone == "R1" and lga_name.upper() == "CANADA BAY":
            alt_rules, _, _ = get_rules_for_lga(lga_name, req.address, "R3")
            alt_rules, alt_matching_zone, alt_excluded, alt_envelope, alt_warnings = _build_scenario(
                alt_rules, "R3", height_open_ended=True, skip_envelope=False,
            )
            alternate_scenario = {
                "label": "If building multi-dwelling housing / a residential flat building instead (Part F)",
                "note": (
                    "R1 General Residential permits both a single dwelling house (the default result "
                    "above, under DCP Part E) AND multi-dwelling housing / residential flat buildings "
                    "(under DCP Part F) — the zone code alone doesn't say which one applies to this "
                    "site, so both are shown. Pick whichever matches what's actually being proposed."
                ),
                "envelope": alt_envelope,
                "envelope_fallback_warnings": alt_warnings,
                "rules_applied": _build_citations(alt_rules, alt_matching_zone, alt_excluded),
            }

        source_docs_used = {r.get("source_document") for r in citations if r.get("source_document")}
        data_currency = {
            doc: DCP_SOURCE_VERSIONS[doc] for doc in source_docs_used if doc in DCP_SOURCE_VERSIONS
        }
        map_data_age_days = None
        if cached_lot.get("map_data_fetched_at"):
            map_data_age_days = round((time.time() - cached_lot["map_data_fetched_at"]) / 86400, 1)

        response = {
            "address":       req.address,
            "lat":           lat,
            "lon":           lon,
            "lga":           lga_label,
            "zone":          zone,
            "lot_polygon":   polygon,
            "envelope":      envelope,
            "envelope_fallback_warnings": fallback_warnings,
            "rules_applied": citations,
            "alternate_development_scenario": alternate_scenario,
            "heritage":      heritage_info,
            "data_currency": {
                "dcp_versions": data_currency,
                "live_map_data_age_days": map_data_age_days,
                "live_map_data_refreshed_every_days": MAP_DATA_TTL_S // 86400,
            },
        }
        if is_five_dock_carveout:
            response["envelope_note"] = (
                "This address is R3 land within the Five Dock Town Centre area bounded by "
                "Barnstaple Road, Waterview Street or Second Avenue — governed by DCP Part G, "
                "not Part F. No envelope shape is drawn: Part G sets setback and height per lot "
                "via colour-coded maps (Figs G3.46, G3.47, G3.49, G3.52), not text values, and "
                "those can't be resolved from an address the way the state LEP height/FSR maps "
                "can. The citations below are the standards that ARE address-independent "
                "(private open space, floor heights, the 6.0m default upper-storey setback, the "
                "height/storey reference table). Setback and exact height for this specific lot "
                "must be checked against Council's maps directly."
            )
        return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cdc-eligibility")
def get_cdc_eligibility(req: EnvelopeRequest):
    """
    Complying Development Certificate (CDC) eligibility + standards under
    Codes SEPP 2008 Part 3 (Housing Code — dwelling houses) and Part 3B
    (Low Rise Housing Diversity Code — dual occupancies, manor houses,
    multi dwelling housing/terraces) — a completely separate consent
    pathway to the DA-based envelope /envelope computes. A CDC is a
    fast-track, privately (or council) certifiable approval with NO
    discretion to vary any standard — if a lot or proposal fails any gate,
    it simply isn't available under this code (a standard DA is still
    possible, just not this shortcut).

    This is deliberately NOT merged into /envelope's citations: the
    numbers here (e.g. cdc_side_setback_min) are a different legal
    standard to the DCP-sourced ones (e.g. side_setback_ground), from a
    pathway most Canada Bay applicants aren't using, and conflating the
    two would misrepresent which standard actually applies to a DA.
    """
    try:
        lat, lon, zone, polygon, lga_name, lga_label, pdf_doc = _resolve_lot(req.address)

        if lga_name.upper() != "CANADA BAY":
            raise ValueError("CDC standards are currently only extracted for Canada Bay.")

        from shapely.geometry import Polygon as ShapelyPolygon
        lot_area_sqm = round(ShapelyPolygon(polygon["coordinates"][0]).area * (111000 ** 2), 1)

        # Housing Code (Part 3: dwelling houses) applies to R1/R2/R3/R4/RU5.
        # Low Rise Housing Diversity Code (Part 3B: dual occ/manor
        # house/terraces) applies to R1/R2/R3/RU5 only — NOT R4 — since
        # these building types cap out at 2 storeys.
        eligibility = {
            "zone": zone,
            "housing_code_zone_eligible": zone in ("R1", "R2", "R3", "R4", "RU5"),
            "low_rise_housing_diversity_code_zone_eligible": zone in ("R1", "R2", "R3", "RU5"),
            "lot_area_sqm": lot_area_sqm,
            "lot_width_check": "not determinable from available data — frontage width must be confirmed manually",
            "corner_or_battle_axe_check": "not determinable from available data — additional gates apply if this is a corner or battle-axe lot (see standards below)",
            "overall_note": (
                "This is a preliminary screen only, not a determination. A CDC also requires "
                "lawful road access and NOT being on land excluded under clause 1.19 (e.g. "
                "environmentally sensitive land), plus the dwelling-count, lot-area and lot-width "
                "gates specific to each code shown in the standards below, and the bush fire / "
                "flood control lot extra requirements where applicable."
            ),
        }

        rules_source = RULES if zone in ("R1", "R2") else RULES_R3
        cdc_zone_filter = "R2" if zone == "R1" else zone
        cdc_rules = [
            r for r in rules_source
            if (
                r.get("parameter", "").startswith("cdc_")
                or (r.get("parameter", "").startswith("cdc3b_") and eligibility["low_rise_housing_diversity_code_zone_eligible"])
            )
            and r.get("zone") in (cdc_zone_filter, "all")
            and not r.get("superseded_by")
        ]

        # Group by (parameter, dwelling_type) rather than parameter alone —
        # Part 3B reuses parameter names (e.g. cdc3b_side_setback_min)
        # across dual occupancy / manor house / terraces with genuinely
        # different values, and collapsing those into one group would bury
        # real differences behind a single "primary" pick.
        grouped: dict[tuple, dict] = {}
        for r in cdc_rules:
            key = (r["parameter"], r.get("dwelling_type", "all"))
            entry = _make_rule_entry(r)
            if key not in grouped:
                grouped[key] = {**entry, "parameter": r["parameter"], "variants": []}
            else:
                grouped[key]["variants"].append(entry)

        source_docs_used = {r.get("source_document") for r in cdc_rules if r.get("source_document")}

        return {
            "address": req.address,
            "lat": lat, "lon": lon,
            "lga": lga_label,
            "zone": zone,
            "pathway": "Complying Development Certificate — Codes SEPP 2008 Part 3 (Housing Code) and Part 3B (Low Rise Housing Diversity Code)",
            "eligibility": eligibility,
            "standards": list(grouped.values()),
            "data_currency": {
                "dcp_versions": {
                    doc: DCP_SOURCE_VERSIONS[doc] for doc in source_docs_used if doc in DCP_SOURCE_VERSIONS
                },
            },
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
    (min lot size, min lot width) using the cached lot polygon. Does a real
    live spatial check against the NSW Town Centres Map for inner/outer LMR
    status (see check_lmr.get_lmr_status).

    Chapter 6 cl 164(1) excludes several other categories of land from LMR
    entirely — heritage items, land Chapter 5 (TOD) applies to, bushfire
    prone land, coastal vulnerability areas, specific flood planning areas,
    ANEF/ANEC noise contours, land near certain pipelines, Accelerated TOD
    Precincts, and the Low and Mid Rise Housing Exclusion Map. Only the
    heritage exclusion is checked here (we already have a live heritage
    layer wired up for other endpoints); the rest are not verified — a
    positive eligibility result here is provisional, not final.
    """
    try:
        lat, lon, zone, polygon, _, _, _ = _resolve_lot(req.address)
        lot_data = {"lat": lat, "lon": lon, "zone": zone, "polygon": polygon}
        result = check_lmr_eligibility(req.address, lot_data, SEPP_RULES)

        heritage_info = (get_cached_lot(req.address) or {}).get("heritage")
        if heritage_info:
            result["lmr_zone_eligible"] = False
            result["reason"] = (
                f"Heritage item/area ({heritage_info.get('name') or heritage_info.get('category')}) "
                f"— Housing SEPP 2021 cl 164(1)(d) excludes heritage land from Chapter 6 (LMR) entirely."
            )
        result["unchecked_ch6_exclusions"] = (
            "Not verified: Transport Oriented Development Area overlap (cl 164(1)(c) — if this "
            "address is in a TOD Area, Chapter 5 applies instead of Chapter 6, and this result is "
            "wrong), bushfire prone land, coastal vulnerability areas, specific flood planning "
            "areas, ANEF/ANEC noise contours, proximity to certain pipelines, Accelerated TOD "
            "Precincts, Low and Mid Rise Housing Exclusion Map areas. Verify directly with Council "
            "before relying on a positive result."
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
