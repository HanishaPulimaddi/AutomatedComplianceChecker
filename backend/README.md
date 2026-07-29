# Backend

FastAPI service that turns a NSW residential address into a buildable envelope and a cited list
of the DCP/LEP/SEPP rules that produced it. Scoped to Canada Bay Council, zones R1–R4.

## Modules

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app: HTTP endpoints, rule loading and filtering, citation building |
| `nsw_apis.py` | Live calls to NSW government spatial APIs — geocoding, cadastre, zoning, FSR/height/lot-size/heritage map layers, road segments |
| `compute_envelope.py` | Geometry engine (Shapely) that clips the lot polygon by directional setbacks to produce the buildable envelope |
| `check_lmr.py` | Housing SEPP 2021 Chapter 6 (Low/Mid-Rise) eligibility, including a live spatial walking-distance check against the NSW Town Centres Map |
| `merge_rules.py` | DCP-vs-SEPP override resolution for LMR areas. Not yet wired into `main.py` — see Known limitations |

The rule data these modules read (`data/rules_*.json`) is generated separately by
[`pipeline.py`](../pipeline.py) at the project root, driven by
[`pipeline_manifest.json`](../pipeline_manifest.json). This backend consumes that output; it
doesn't extract rules itself.

## Setup

```powershell
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in what you need:

| Variable | Required for |
|---|---|
| `ANTHROPIC_API_KEY` | Rule extraction (`pipeline.py`) only — not needed to run the API |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | Document structure detection (`pipeline.py`) only |
| `FRONTEND_ORIGIN` | CORS origins once the frontend is deployed off `localhost` |

The API itself needs no keys at runtime — it calls NSW's public spatial APIs directly.

## Running

```powershell
uvicorn backend.main:app --reload --port 8000
```

The Vite dev server proxies `/envelope`, `/site`, `/cdc-eligibility`, `/address-suggestions`,
`/grasshopper-plugin`, and `/docs` to `http://localhost:8000`, so run the backend on port 8000
for local frontend development.

Startup requires `data/rules_r2_canada_bay.json`, `data/rules_r3_canada_bay_pipeline.json`,
`data/rules_canada_bay_part_g_five_dock.json`, `data/rules_housing_sepp_ch6.json`, the Part C/E
DCP chunk files, and the `docs/` directory (mounted for citation PDF links) — these load eagerly
at import time. `data/cached_lots.json` and `data/town_centres.geojson` are created lazily on
first use if missing.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Health check |
| `GET` | `/grasshopper-plugin` | Downloads the Grasshopper definition |
| `GET` | `/address-suggestions?q=` | Autocomplete, biased toward Canada Bay |
| `GET` | `/chunks?q=&part=&limit=` | Keyword search over raw DCP text chunks |
| `POST` | `/site` | Lot polygon, zone, and LGA for an address |
| `POST` | `/envelope` | Main endpoint — buildable envelope and full rule citations for an address |
| `POST` | `/cdc-eligibility` | Complying Development Certificate fast-track eligibility (Codes SEPP 2008) |
| `POST` | `/lmr` | Housing SEPP 2021 Ch6 Low/Mid-Rise eligibility, including a live spatial check |

`/site`, `/envelope`, `/cdc-eligibility`, and `/lmr` all take `{"address": "..."}` as the request
body. `/envelope` additionally returns an `alternate_development_scenario` for R1 lots, since R1
zoning permits both a single dwelling house and multi-dwelling housing on the same zone code.

### `/envelope` response (abridged)

```jsonc
{
  "address": "...", "lat": ..., "lon": ..., "lga": "Canada Bay Council", "zone": "R2",
  "lot_polygon": { "type": "Polygon", "coordinates": [...] },
  "envelope": { "type": "Polygon", "coordinates": [...] },
  "rules_applied": [
    { "parameter": "front_setback", "value": 4.5, "unit": "m", "operator": "min",
      "clause": "E4.2 C1", "page": 6, "text": "...", "conditions": [...], "exceptions": [...],
      "variants": [...], "pdf_link": "/docs/canada-bay-dcp-part-e-dwelling-houses.pdf#page=6",
      "confidence": 0.95, "verified": false }
  ],
  "alternate_development_scenario": null,
  "heritage": null,
  "data_currency": { "dcp_versions": {...}, "live_map_data_age_days": 3.2, "live_map_data_refreshed_every_days": 30 }
}
```

If a required setback rule genuinely can't be matched for a lot, `envelope` comes back `null`
with an `envelope_note` explaining why, rather than substituting a guessed value — the design
principle throughout the API is that an honest gap is preferable to a confidently wrong number.

## Known limitations

Most of these are deliberate scope decisions rather than bugs:

- Corner-lot secondary-frontage setback detection is implemented but disabled by default — an
  early test produced a false positive on a real non-corner lot, so it's off until validated
  further against confirmed corner-lot addresses.
- Tiered rules (site coverage %, FSR tiers) are returned as citation variants rather than
  resolved to the one tier that applies to the lot; lot area isn't yet threaded into rule
  selection.
- The Five Dock Town Centre precinct has no computed envelope — its controls are set by
  colour-coded DCP figures rather than text values that can be resolved from an address.
- Inner West Council (Marrickville/Ashfield) rule data exists and is documented in
  `../ACCURACY.md`, but isn't currently routed — only Canada Bay is served.
- Most Housing SEPP Ch6 exclusions (TOD overlap, bushfire, coastal, flood, noise, pipelines)
  aren't checked in `/lmr`; only the heritage exclusion is verified live.
- State-level SEPP overrides for LMR-eligible areas aren't yet merged into `/envelope`'s
  figures — `merge_rules.py` contains the resolution logic but isn't wired in.
- CDC lot-width and corner/battle-axe checks are flagged as requiring manual confirmation
  rather than computed.

See `../ACCURACY.md` for the full extraction accuracy report against verified ground-truth
addresses.
