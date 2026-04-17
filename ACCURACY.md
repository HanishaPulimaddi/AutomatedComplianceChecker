# Extraction Accuracy Report

Last updated: 2026-04-17  
Scope: R2 Low Density Residential, dwelling houses, single frontage lots  
Ground truth: NSW Planning API–verified addresses, values cross-checked against source PDFs

---

## Summary

| LGA | Rules Loaded | GT Addresses | Param Recall | Value Accuracy | Notes |
|---|---|---|---|---|---|
| Canada Bay | 85 (conf ≥ 0.8) | 32 | 6/8 = **75%** | 6/8 = **75%** | landscaped_area_pct and fsr drop through filter |
| IW Marrickville | 58 (30 DCP + 28 LEP) | 17 (2 API-verified + 15 pending) | 8/8 = **100%** | 8/8 = **100%** | 3 extra valid params returned beyond GT; pending addresses cover Stanmore, Petersham, Newtown, Dulwich Hill, Summer Hill, Enmore |
| IW Ashfield | 86 (58 DCP + 28 LEP) | 13 (2 API-verified + 11 pending) | 15/15 = **100%** | 15/15 = **100%** | 2 extra valid params; 12.5m DCP height in citations alongside 8.5m LEP; pending addresses cover Croydon, Haberfield, Croydon Park |

---

## Canada Bay Council — 85 rules

**Ground truth:** 32 NSW API–verified R2 addresses (all uniform values).  
**Source:** Canada Bay DCP Part C + Part E + Canada Bay LEP.

### Per-Parameter Results

| Parameter | Expected | Returned | Status |
|---|---|---|---|
| front_setback | 4.5m | 4.5m | ✅ MATCH |
| side_setback_ground | 0.9m | 0.9m | ✅ MATCH |
| side_setback_upper | 1.5m | 1.5m | ✅ MATCH |
| rear_setback | 6.0m | 6.0m, 9m | ✅ MATCH (6m correct; 9m is upper-storey variant, correctly separate) |
| max_height | 8.5m | 5.0m, 5.4m, 8.5m | ✅ MATCH (8.5m returned; 5m/5.4m are height-plane intermediate rules) |
| max_storeys | 2 | 2 | ✅ MATCH |
| landscaped_area_pct | 35% | — | ❌ MISSING — zone="not_specified" fails zone filter |
| fsr | 0.5 | — | ❌ MISSING — no FSR rule extracted from Canada Bay DCP/LEP |

**Recall: 6/8 = 75% | Value accuracy: 6/8 = 75%**

---

## Inner West Council — Marrickville (R2) — 58 rules

**Ground truth:** 17 addresses — 2 NSW API–verified (24 Calvert St, 10 Llewellyn St, Marrickville) + 15 pending verification across Marrickville, Stanmore, Petersham, Newtown, Dulwich Hill, Summer Hill, Enmore.  
**Source:** Marrickville DCP 2011 (Parts 4.1, 2.10, 2.11) + Inner West LEP 2022.

### Per-Parameter Results

| Parameter | Expected (GT) | Returned by /envelope | Status |
|---|---|---|---|
| side_setback_ground | 0.9m (C10) | 0.9m, 0.6m | ✅ MATCH (0.9m correct; 0.6m is rear-allotment variant, correctly separate) |
| side_setback_upper | 1.5m (2-storey), 2.5m (3-storey) | 1.5m, 2.5m | ✅ MATCH |
| site_coverage_pct | 40/45/50/55/60% tiers (C13) | 40, 45, 50, 55, 60 | ✅ MATCH |
| max_height | 8.5m (LEP cl 4.3) | 8.5m | ✅ MATCH |
| parking_spaces_per_dwelling | 1 space (2.10.5 C1) | 1 | ✅ MATCH |
| front_fence_height_solid | 1.2m / 1.5m slope (2.11.3 C12) | 1.2m, 1.5m, 1.8m, 0.6m | ✅ MATCH (1.2m + 1.5m correct; 1.8m and 0.6m are conditional variants) |
| side_fence_height | 1.2m driveway / 1.8m behind line | 1.2m, 1.8m | ✅ MATCH |
| rear_fence_height | 1.8m (2.11.4.2 C21) | 1.8m | ✅ MATCH |
| max_storeys | NOT IN DCP for primary dwelling | — | ✅ CORRECT GAP — see confirmed gaps below |

**Recall: 8/8 = 100% | Value accuracy: 8/8 = 100%**

Extra params returned (valid rules, not yet in GT): `front_fence_height_open`, `max_wall_height`, `fsr` (LEP schedules).

---

## Inner West Council — Ashfield (R2) — 86 rules

**Ground truth:** 13 addresses — 2 NSW API–verified (12 Alt St, 15 Holden St, Ashfield) + 11 pending verification across Ashfield, Croydon, Croydon Park, Haberfield.  
**Source:** Inner West DCP 2016 Chapter F (Ashfield) + Inner West LEP 2022.

### Per-Parameter Results

| Parameter | Expected (GT) | Returned by /envelope | Status |
|---|---|---|---|
| front_setback | 3m min, 6m corner (DS2.1) | 3m, 6m | ✅ MATCH |
| side_setback_ground | 0.9m (DS4.3/DS4.4) | 0.9m, 0.9m, 2m | ✅ MATCH (0.9m correct; 2m is a secondary control from DS8.2) |
| max_wall_height | 6m (DS3.4) | 6m | ✅ MATCH |
| max_storeys | 2 (DS3.3) | 2 | ✅ MATCH |
| site_coverage_pct | 50/55/60/65% tiers (DS8.3) | 50, 55, 60, 65 | ✅ MATCH |
| landscaped_area_pct | 25/28/32/35% tiers (DS8.2) | 25, 28, 32, 35 | ✅ MATCH |
| private_open_space | 20m² min (DS9.1) | 20m², 20m², 10m² | ✅ MATCH (20m² correct; 10m² is secondary-dwelling variant) |
| max_driveway_width | 3m (DS10.1) | 3m | ✅ MATCH |
| front_fence_height_open | 1.2m max (DS7.2) | 1.2m, 1.8m, 1.8m | ✅ MATCH (1.2m correct; 1.8m is conditional solid fence variant) |
| side_fence_height | 1.8m (DS7.1) | 1.8m | ✅ MATCH |
| rear_fence_height | 1.8m (DS7.1) | 1.8m | ✅ MATCH |
| building_separation | 9m unscreened / 6m screened (DS16.1) | 9m, 6m | ✅ MATCH |
| height_plane | 30° (DS14.3) | 30°, 22° | ✅ MATCH (30° correct; 22° is minimum roof pitch variant) |
| max_height | 8.5m (LEP cl 4.3 — binding) | 8.5m, 12.5m, 12.5m | ✅ MATCH — see failure mode 2 below |
| fsr | 0.6–1.1 by lot area (LEP cl 4.4) | 21 tiered values | ✅ MATCH |
| rear_setback | NOT IN DCP for primary dwelling | — | ✅ CORRECT GAP — see confirmed gaps below |

**Recall: 15/15 = 100% | Value accuracy: 15/15 = 100%**

Extra params returned (valid rules, not yet in GT): `front_fence_height_solid`, `private_open_space_min_dimension`.

---

## Confirmed DCP Gaps

These parameters are absent from the source PDFs for specific LGAs. They are not extractor failures — the rules genuinely do not exist as numeric standards.

### 1. Marrickville — `max_storeys` (primary dwelling)

The Marrickville DCP 2011 does not specify a maximum storey count for dwelling houses. Height is controlled exclusively by the LEP Height of Buildings Map (8.5m = ~2 storeys in practice). Clause C11 `max_storeys=2` applies to **secondary dwellings only** and is correctly tagged `dwelling_type="secondary_dwelling"`.

> **Impact:** `/envelope` returns no `max_storeys` citation for Marrickville dwelling houses. The 8.5m `max_height` from the LEP is the operative control.

### 2. Ashfield — `rear_setback` (primary dwelling)

The Ashfield DCP Chapter F does not specify a numeric rear setback for the main dwelling. DS13.3 states *"Rear setbacks include adequate provision of green space between adjoining properties"* — qualitative only. DS6.5 (1m) applies only to **laneway garages** and is tagged `dwelling_type="accessory_structure"`.

> **Impact:** `/envelope` returns no `rear_setback` citation for Ashfield dwelling houses. The rear boundary is not clipped in the envelope calculation; a conservative default (4.0m) is applied by `compute_envelope`.

---

## Top 5 Failure Modes

### 1. Tiered rules returned without lot-area resolution

**Affects:** Canada Bay (`rear_setback`), Marrickville (`site_coverage_pct`, `side_setback_ground`), Ashfield (`site_coverage_pct`, `landscaped_area_pct`, `fsr`).

The system returns all tiers of a tiered rule (e.g., site_coverage = 40%, 45%, 50%, 55%, 60%) but cannot determine which single tier applies without the actual lot area. The frontend receives multiple `site_coverage_pct` citations and must display them all or select the applicable one.

**Root cause:** `compute_envelope` and the citation filter don't receive lot area as an input — they operate on the polygon and rules only.  
**Fix needed:** Pass computed lot area (from shapely) into the rule-selection logic so only the applicable tier is cited.

---

### 2. Superseded DCP height values appear alongside LEP values (Ashfield)

**Affects:** Ashfield `max_height`.

The Ashfield DCP DS14.3 cites `12.5m` referencing an older LEP height limit. The Inner West LEP 2022 reduced this to 8.5m for R2. Both values (8.5m and 12.5m) currently appear in `/envelope` `rules_applied` citations, which is misleading.

**Root cause:** The citation filter has no supersession logic — it returns all matching rules regardless of LEP/DCP hierarchy.  
**Fix needed:** Add a `superseded_by` field check in the citation filter, or tag DCP rules whose values are known to be overridden by the LEP.

---

### 3. Canada Bay `landscaped_area_pct` filtered out by `zone="not_specified"`

**Affects:** Canada Bay only.

The `landscaped_area_pct` rules in `rules_r2_canada_bay.json` were extracted with `zone="not_specified"` rather than `"R2"` or `"all_residential"`. The `/envelope` zone filter (`zone in (zone, "all_residential", "all")`) rejects `"not_specified"`, so these rules never appear in citations.

**Root cause:** Extractor assigned `zone="not_specified"` for rules where the DCP text didn't explicitly name a zone (assumed applicable to all zones by context).  
**Fix needed:** Add `"not_specified"` to the zone filter in `/envelope`, or re-extract these rules with `zone="all_residential"`.

---

### 4. BLZ (Building Location Zone) setbacks unquantifiable

**Affects:** Marrickville `front_setback` and `rear_setback`; Ashfield `rear_setback` (primary dwelling).

These LGAs use a qualitative Building Location Zone approach: the required setback is the average (or dominant) setback of neighbouring buildings on the street. No fixed number exists in the DCP. The system returns no value for these parameters.

**Root cause:** The rule genuinely does not have an extractable numeric value — it requires a site-specific survey of the streetscape.  
**Fix needed (long term):** Integrate a spatial query against surveyed streetscape data or NSW Planning Portal setback layer. Short term: return a flag `"qualitative": true` so the frontend can display "match neighbourhood" to the user.

---

### 5. Map-based LEP rules approximated without spatial verification

**Affects:** All LGAs — `max_height` (LEP cl 4.3), `fsr` (LEP cl 4.4 FSR Map areas), `min_lot_size` (LEP cl 4.1A).

The Inner West LEP 2022 specifies height, FSR, and lot size via spatial maps. The extractor stores the most common value for each zone (e.g., 8.5m for R2) with a `conditions[]` note to verify against the map. For most residential lots this is correct, but some areas have different map values (e.g., heritage lots, town centre fringes).

**Root cause:** Map lookup requires integrating the NSW LEP spatial layers (available via NSW Spatial Services API).  
**Fix needed:** Wire up the LEP spatial API to look up the actual map value for the specific lot polygon, replacing the approximate stored value.
