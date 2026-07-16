# Arrayah Compliance Copilot — Project Report

*Automated Compliance Checker: address → buildable envelope, for NSW residential development*

---

## 1. Background & Context

Every residential development application (DA) in NSW must comply with a stack of planning instruments layered on top of each other:

- **LEP (Local Environmental Plan)** — statutory, sets the binding maximums (height, floor space ratio, minimum lot size) via spatial maps.
- **DCP (Development Control Plan)** — council-specific design controls (setbacks, landscaped area, private open space, parking) written as PDF text, structured differently by every council.
- **SEPP (State Environmental Planning Policy)** — state-level overrides. Two matter here: **Codes SEPP 2008** (fast-track Complying Development Certificate pathway) and **Housing SEPP 2021 Chapter 6** (Low and Mid Rise housing reforms).

None of these are published as structured data. An architect or planner has to manually cross-reference a LEP map, a 200+ page DCP PDF, and applicable SEPP clauses, by hand, for every single site — and get it right, because a wrong setback can mean a DA rejection or a built structure that doesn't comply.

In February 2025, NSW state government overrode local DCP controls for the Low and Mid Rise housing reforms across 177 suburbs — a live example of exactly how fast this rule stack can shift, and how much manual re-checking that forces onto anyone doing planning work in NSW.

## 2. Problem Statement

**"What can I build on this lot?"** is a question that currently takes an architect days to weeks to answer correctly, because the answer is scattered across unstructured PDFs, spatial map layers, and overlapping state/council rules with no single authoritative, machine-readable source. The cost isn't just time — an error in reading a setback or height limit by hand can result in a non-compliant design that only gets caught (or doesn't) at DA lodgement or construction.

## 3. Motivation

The project's origin is a direct response to that problem: one of the two founders (an architecture-trained software engineer) had firsthand experience spending weeks reading DCP/LEP PDFs before being able to start designing. The other founder, coming from a data background, recognized this as a structured-extraction problem that LLMs were newly capable of solving reliably. The February 2025 NSW housing reform override made the timing pointed: council rules were already being rewritten faster than manual processes could track them, creating real urgency for an automated, always-current answer.

The founding bet: **build the tool an architect would actually use** — not a web dashboard that adds a new tool to their workflow, but something that plugs directly into Rhino/Grasshopper, where architects already do their massing studies.

---

## 4. Technical Architecture

Two halves, evolving together:

### 4.1 The Model (backend)

```
Address
  │
  ▼
NSW ArcGIS Geocoder ──► lat/lon
  │
  ▼
NSW Cadastre API ──► lot polygon (GeoJSON)
NSW Planning API ──► zone, LGA
NSW LEP Map Layers ──► FSR, height-of-building, min lot size, heritage (live, per-lot)
  │
  ▼
Rule lookup (DCP + LEP + SEPP, filtered by zone/dwelling type)
  │
  ▼
compute_envelope() ──► buildable footprint polygon + citations
  │
  ▼
FastAPI JSON response ──► frontend map UI / Grasshopper plugin
```

- **`backend/nsw_apis.py`** — all external API integration: ArcGIS geocoding and autocomplete, NSW Six Maps cadastre (lot polygons), NSW Planning spatial layers (zone, LGA, FSR, height, lot size, heritage), NSW Transport road-segment layer (used for corner-lot frontage detection).
- **`backend/compute_envelope.py`** — the geometry engine. Takes a lot polygon + a filtered rule set and produces a clipped buildable-envelope polygon.
- **`backend/check_lmr.py`** — Housing SEPP 2021 Ch6 LMR eligibility: zone check, spatial distance-to-town-centre check (walking distance via NSW Town Centres Map), dimensional checks (min lot size/width).
- **`backend/main.py`** — FastAPI app; wires the above together into `/site`, `/envelope`, `/cdc-eligibility`, `/lmr`, `/address-suggestions`, `/chunks`, `/grasshopper-plugin`.
- **`pipeline.py`** — the LLM-driven rule extraction pipeline: detects a DCP's document structure, chunks the PDF with section context, extracts structured rules (parameter, value, unit, operator, conditions, exceptions, source clause/page), validates and deduplicates.

### 4.2 The Plugin (Grasshopper/Rhino)

- **`RhinoPlugInV1.gh`** — a Grasshopper definition with a Python component that calls the same FastAPI backend an architect would otherwise hit via the web UI.
- **`grasshopper_plugin.py`** — a readable, standalone copy of the code embedded in that component (Grasshopper's own Python component isn't independently runnable/diffable, so this file exists purely so the logic can be read and edited outside Rhino).
- Converts the backend's GeoJSON lot/envelope polygons into real Rhino curves, extrudes a 3D mass from the envelope up to the height limit, and surfaces citations, LMR info, and development controls as labelled outputs directly in the Rhino viewport.
- The core design principle, learned the hard way (§6.1): **the plugin consumes backend-computed values, it does not recompute them.** Every number an architect sees in Rhino traces back to the same `compute_envelope()` call the API serves.

---

## 5. Evolution: How Both Halves Got Here

### Period 1 — Initial build sprint (8–22 April 2026, ~2 weeks)

This is the period the original project summary describes, and the commit log confirms it closely:

| Day | What shipped |
|---|---|
| Day 1 | Folder structure, source PDFs, ground truth address set, rule schema |
| Day 2 | PDF parser producing 191 clean chunks from DCP Part E; first extraction pass |
| Day 3 | LLM extraction complete — 72 valid rules from DCP Part E |
| Day 4 | FastAPI backend live (`/site`, `/envelope`) against 3 test addresses; 78 rules verified, 8/8 ground truth, 94% F1 |
| Day 5 | Verified rules connected to backend; citation formatting added |

From there the scope expanded fast: Housing SEPP Ch6 rules (manually extracted), a bulk address-fetch/caching script, front-edge detection fixed to use nearest-boundary-point instead of closest-midpoint, directional setbacks, SEPP override merge logic, and — significantly — **Inner West Council** support added (Marrickville + Ashfield +, briefly, Leichhardt). Leichhardt was removed within the same week once its suburbs turned out to be zoned R1 (outside the tool's R2 scope) — an early, correct instance of scope discipline that the team would apply again, more drastically, later. The sprint closed with backend fixes "before Demo Day" (22 April) — normalising list/dict response shapes, fixing missing lat/lon handling, restoring `compute_envelope.py` after a merge conflict.

**Grasshopper plugin work also happened in this window** (per the original project summary): the Python component, GeoJSON→Rhino curve conversion, 3D mass extrusion, and the 7-output layout (lot curve, envelope curve, 3D mass, citations, LMR info, controls, constraint labels). The "area returning 0" bug (§6.1) and the GH/Rhino document-context bug (§6.5) both date from this period.

### Period 2 — the pivot: scaling down to go deep (5–15 July 2026)

Development resumed with a different priority entirely from the original roadmap — not growth to more councils, but **consolidation, and going deep on one council instead of shallow across several**.

**Why the pivot happened:** both founders come from a technical/software background, with no formal architectural or planning domain background. As the project's scope grew — multiple councils, each with differently-structured DCPs, plus LEP, plus two SEPPs, plus the geometry engine underneath all of it — it became genuinely difficult to know what they didn't know. Without domain expertise to sanity-check the output, the team couldn't reliably tell which parts of a fast-expanding system were solid and which had quiet loopholes, because nobody on the team had the planning background to know what a "wrong" answer even looked like versus a merely-unfamiliar one. Rather than keep expanding a system they couldn't fully audit themselves, the team made the deliberate call to **scale back down to a single council** and get that one working properly enough to stand behind — a smaller, well-understood outcome over a larger, uncertain one.

- **5 July** — *"Add R1 dual-scenario support, Canada-Bay-only pipeline cleanup, and UX fixes."* This is the pivotal commit. Inner West/Marrickville/Ashfield extraction code **and data were deliberately removed**. The per-council `extractor/` scripts (`extract_rules_iw.py`, `parse_pdf_iw.py`, `extract_rules_lep_iw.py`) were dropped in favour of one generic `pipeline.py`. R1 zoning got proper dual-scenario handling (a dwelling house under DCP Part E *and* an alternate multi-dwelling scenario under Part F, since R1's own LEP land-use table permits both and the zone code alone doesn't say which an applicant means). PDFs and the Grasshopper plugin download were fixed to be real served files instead of dead links.
- **6–7 July** — a fix, a revert, and a re-fix of the R1 CDC zone filter. The revert ("undocumented in PRD, needs author confirmation") is worth calling out specifically: it's evidence of the same discipline behind the pivot — an uncertain change was pulled back out rather than shipped and hoped for, then re-applied correctly once resolved.
- **15 July (this engagement)** — *"Fix envelope area/geometry accuracy, archive unused rule data, add CDC test suite."* Detailed in §7 below — this is where the two real correctness bugs (longitude-compression setback error, and the flat-footprint/height-plane gap) were found and fixed, a geometry safety guard was added, the accuracy test suite was extended to actually check envelope geometry (not just rule values), and nine orphaned data files plus one dead module were archived.

**In hindsight, the pivot was the right call, for a reason beyond the one that motivated it.** It wasn't just that the team lacked domain expertise to audit a fast-expanding system — it turns out there really were loopholes hiding in it. This engagement independently found a geometry bug that had been silently mis-applying setbacks by up to 17% on certain lot orientations since the original build, undetected through the entire multi-council expansion. Scaling that engine to 10 more councils would have multiplied a real accuracy problem, not just a coverage gap. The decision to stop, descope, and get one council right before going wider holds up.

---

## 6. Challenges Encountered and How They Were Solved

Combining the original project summary's documented challenges with what this engagement independently found and verified against the actual code — corrected where the original description didn't match what was actually shipped.

### 6.1 Envelope area returning 0 in Grasshopper (Period 1)

**Problem:** Backend correctly computed 141.1 m², Grasshopper showed 0.0, the 3D mass had no volume.
**Root cause:** `development_controls` wasn't returned from `/envelope`; Grasshopper was trying to recompute area from the converted Rhino surface, and `AreaMassProperties.Compute()` returned 0 for that surface.
**Fix:** Return `development_controls` from the API; make Grasshopper consume the backend's area directly instead of recalculating.
**Lesson (validated, still holds):** backend calculations are the source of truth; the plugin should consume, not recompute.

### 6.2 Directional setbacks vs. a naive uniform buffer (Period 1)

**Problem:** An initial uniform-buffer approach averaged all four setbacks — `(4.5 + 6.0 + 0.9 + 0.9) / 4 ≈ 3.08m` — applied identically to every edge. Wrong by design: front setback (4.5m) and side setback (0.9m) are not interchangeable.
**Fix:** `_apply_directional_setbacks()` — for each edge, compute its inward perpendicular normal, offset by *that edge's own* setback, build a half-plane on the inward side, and intersect the lot polygon with it edge-by-edge. Front, rear, and side setbacks are now genuinely distinct.
**Note on the original "±0.1% accuracy, validated against hand calculations" claim:** this described the *edge-differentiation* concept correctly, but see §6.3 — the actual shipped implementation had a separate, undiscovered unit-conversion bug that meant true accuracy was nowhere near ±0.1% until this engagement's fix.

### 6.3 Coordinate conversion — the bug the original documentation described as solved, but wasn't (found and fixed in this engagement)

**What the original documentation claimed:** that a `cos(latitude)` correction was already applied when converting the setback offset vector from metres to degrees, to account for longitude degrees being physically shorter than latitude degrees away from the equator.

**What the shipped code actually did:** `compute_envelope.py` used a single flat constant, `M_TO_DEG = 1 / 111000`, applied identically to both the north-south and east-west components of every offset. No `cos(latitude)` term existed anywhere in the setback-clipping path. At Sydney's latitude (≈ -33.85°, cos ≈ 0.83), this **silently under-applied setbacks by up to ~17% on east/west-facing lot edges** — a 4.5m required front setback could clip as little as ~3.7m in reality, meaning the tool could show a legal-looking envelope that was actually larger than what council rules permit.

**The fix (this engagement):** rewrote the setback-application path to project the lot polygon into a local metric coordinate system (`_project_to_m`, using `111,320m × cos(latitude)` for longitude and `111,000m` for latitude) before doing *any* offset or area math, then project the result back. This also surfaced and fixed the same bug hiding in three other places that all independently computed area from raw lon/lat degrees: `compute_development_controls`'s `_area_sqm()`, `main.py`'s CDC lot-area calculation, and `check_lmr.py`'s `compute_lot_dimensions()`.

**A second bug found while fixing the first:** a hardcoded `BIG = 2.0` constant (used to build an oversized half-plane rectangle for clipping) was tuned for degree-space, where a lot spans ~0.0003°. Once coordinates were correctly projected to metres, that same constant became a comically small 2 metres — smaller than any real lot — which silently corrupted every single envelope computation. Caught immediately by testing against all 54 cached lots (100% failure rate before the fix, 0% after). Rescaled to `5000.0`.

**Verification:** re-tested against 54+ cached lots, re-ran the existing rule-accuracy suite (100%/100% maintained, unaffected since it doesn't test geometry), and confirmed visually in a live browser session — lot area, footprint area, and coverage % all matched between the backend computation and the rendered map.

### 6.4 Rule extraction accuracy (Period 1, revisited)

**Problem:** initial extraction had incorrectly-merged conditional rules, non-standardised units (900mm vs 0.9m), and ambiguous parameter names.
**Fix:** a validation pipeline — deduplication (by parameter + value + dwelling type + zone + clause), a confidence threshold (0.8), and schema validation against a fixed set of valid parameters/units/operators/dwelling-types.
**Correction to the original claim:** the original summary attributes this extraction to "Claude/Gemini" and lists "Claude-powered rule reading" as a competitive advantage. The actual current `pipeline.py` extracts using **Gemini and Groq only** — the Anthropic client and `claude-sonnet-4-6` model were explicitly removed in an April 2026 commit and replaced with Gemini, evidently to avoid Anthropic API cost during heavy iteration. `ANTHROPIC_API_KEY` and the `anthropic` package are unused dead weight in the current repo (removed from `requirements.txt` in this engagement). If Claude-based extraction is a claimed differentiator going forward, it needs to actually be reinstated — it isn't running today.
**A second, separate gap found in this engagement:** `pipeline.py`'s own `validate()` correctly flags schema-invalid rules, but flagged rules are still written into the same output file as valid ones (`all_out = lep_rules + valid + flagged`), and nothing downstream checks the `validation_warnings` field it attaches — only the `confidence ≥ 0.8` threshold gates what's loaded live. Currently zero flagged rules exist in the live data, so this hasn't bitten yet, but it's a live risk for the next extraction run.

### 6.5 Grasshopper/Rhino document-context switching (Period 1)

**Problem:** a colour-assignment script failed inside the Grasshopper Python component with "this type of object is not supported in Grasshopper" — it was trying to create Rhino layers while `sc.doc` pointed at the Grasshopper document, not Rhino's.
**Fix:** explicitly save `sc.doc`, switch it to `Rhino.RhinoDoc.ActiveDoc` for the duration of the layer operations, then restore it.
**Lesson:** Grasshopper and Rhino are separate document contexts; anything touching Rhino-native objects (layers, geometry attributes) from inside a GH component must switch contexts explicitly.

### 6.6 NSW API rate limiting (Period 1)

**Problem:** five sequential API calls per address (geocode, LGA, zone, lot polygon, FSR/height/lot-size/heritage map layers) hit 429s, capping throughput at ~20 addresses/minute.
**Fix:** a 0.5s delay between calls, and a cache (`data/cached_lots.json`) storing the full resolved lot record per address. First request: 2-3s; cached: near-instant. Live map-layer data (FSR/height/lot-size/heritage) is re-fetched if the cached copy is older than 30 days, so a permanent cache doesn't serve stale post-amendment values forever.

### 6.7 Multi-council DCP variation (Period 1 — later reversed)

**Problem:** Canada Bay's DCP structure differs from Inner West's three separate DCPs (Marrickville, Ashfield, briefly Leichhardt); parameter naming and section structure varied by council.
**Original fix:** an early per-council extractor set (`extractor/extract_rules_iw.py`, `parse_pdf_iw.py`, etc.) handling Inner West's variations directly.
**What actually happened next:** rather than generalising this further to more councils (per the original roadmap), the 5 July cleanup **consolidated extraction into one generic `pipeline.py`** driven by a document-structure-detection step (works out a DCP's clause-labelling and heading conventions per document, rather than hardcoding them) — and simultaneously **removed Inner West support entirely**, narrowing live scope back to Canada Bay only. The generic pipeline exists and is designed to generalise; it just isn't currently pointed at any council but Canada Bay in `pipeline_manifest.json`.

### 6.8 Envelope geometry edge-detection weaknesses (found in this engagement, still open)

Building a geometry-focused test into `test_accuracy.py` (measuring the actual clipped envelope's setback distances independently via the haversine great-circle formula, rather than trusting the same projection math being tested) surfaced two further, real, **still-unfixed** issues:

- **Wide/shallow lots swap rear and side setbacks.** The "rear edge = whichever edge's midpoint sits furthest from the front edge's midpoint" heuristic is only correct when a lot's width is less than ~1.73× its depth. Confirmed concretely on real Canada Bay addresses (e.g. a 43.8m-wide, ~13m-deep lot) where a side edge got clipped as "rear" (applying 0.9m where 6.0m was required) and vice versa. Attempted a parallelism-based guard for this, calibrated it against the actual cached dataset, and found it fired on 89% of simple lots — unusable as a warning. Pulled it back out rather than ship noise; the underlying question (is this dataset genuinely skewed wide/shallow, or is front-edge detection itself unreliable for many real addresses) needs real investigation, not a quick threshold.
- **Complex/curved boundaries confuse edge detection entirely.** A real 100-vertex lot (a curved/waterfront cadastral boundary approximated by dozens of ~1.2m micro-segments) had the front-edge detector pick one of those micro-segments as "the front edge" — architecturally meaningless. **This one is fixed**: any lot boundary over 12 vertices now returns its envelope *plus* a clear warning rather than a silently-wrong number, surfaced through the API's `envelope_fallback_warnings` and a dedicated frontend banner.

### 6.9 A ground-floor-only envelope presented as if it were a full 3D massing constraint (found and disclosed in this engagement)

**Problem:** `_apply_directional_setbacks()` only ever accepts front/rear/side(ground)/secondary setbacks — there is no parameter for the upper-storey side setback, and `height_plane` is never applied to the geometry at all. The "3D buildable envelope" the README and pitch materials describe is, in the actual returned polygon, a single flat ground-floor footprint extruded to a flat height — it does not step in for upper floors the way most DCPs require.
**Resolution (this engagement):** rather than attempt a full stepped-massing rebuild (a real geometry project, not a quick fix), added an explicit disclaimer — in the API response (`envelope_geometry_note`) and a matching frontend banner — stating plainly that the shape is ground-floor-only and that `side_setback_upper`/`height_plane` are cited separately but not yet applied to the geometry. Honest disclosure over silent overclaiming.

---

## 7. Major Technical Decisions and Why

| Decision | Reasoning |
|---|---|
| **Directional (per-edge) setbacks over a uniform buffer** | A uniform average is provably wrong whenever front/rear and side setbacks differ significantly, which is the normal case (§6.2). |
| **Project to a local metric coordinate system before any geometry math** | Doing offset/area math directly in lon/lat degrees is only correct at the equator; a proper metric projection (§6.3) is the only way to get real, latitude-independent accuracy. |
| **Generic pipeline.py over per-council extractor scripts** | Council DCPs vary in structure but not in *kind* of variation (clause labels, heading conventions, section nesting) — a structure-detection step generalises better than hardcoding each council's quirks, and is much less code to maintain. |
| **Gemini/Groq over Claude for extraction (as currently implemented)** | Cost during heavy iteration — free-tier models were swapped in specifically to avoid per-call Anthropic API cost while extracting hundreds of rules repeatedly during development. This is a real trade-off, not a strict downgrade, but it does mean any "Claude-powered" claim needs updating or the extraction needs to move back. |
| **Narrowing scope back to Canada Bay only (5 July cleanup)** | Neither founder has a formal architecture/planning background; as the system grew across multiple councils' worth of DCP/LEP/SEPP rules, the team lost the ability to audit their own output and tell a genuine loophole apart from an unfamiliar-but-correct result. Rather than keep building on a foundation they couldn't fully verify themselves, they deliberately scaled down to one council to produce a result they could actually stand behind — validated in hindsight by the real correctness bugs this engagement found (§6.3, §6.8), which had been hiding in the system the whole time it was being expanded. |
| **R1 dual-scenario output instead of picking one interpretation silently** | R1 General Residential's own LEP land-use table permits both a single dwelling house and multi-dwelling/residential flat buildings — the zone code alone doesn't disambiguate, so the tool computes and clearly labels both rather than guessing. |
| **A disclaimer over a silent wrong number, applied twice (ground-floor-footprint, and the vertex-count geometry guard)** | For a compliance tool, a confidently-wrong number is strictly worse than an honestly-flagged uncertain one — the two disclaimers added in this engagement both follow this same principle. |
| **Archiving rather than deleting superseded data/code** | Nine data files and one dead module were found to be unreferenced by any code path; moved to `archive/` (with an explanatory README) rather than deleted outright, since this is a reversible, low-risk action appropriate for a "wrap up and document" phase rather than a "we're certain we'll never need this" phase. |

---

## 8. Scope, Limitations, Constraints, Dependencies

**Current scope:** City of Canada Bay Council only; R1–R4 residential zones. Every other LGA returns a 404. Inner West (Marrickville/Ashfield) support existed briefly and was deliberately removed — not a bug, a scope decision.

**Known, documented limitations:**
- The envelope is a ground-floor footprint only (§6.9) — upper-storey setback and height plane are cited but not geometrically applied.
- Wide/shallow lots and lots with complex/curved boundaries can have unreliable front/rear/side edge detection (§6.8) — the latter is now flagged automatically, the former is not yet.
- Small R3/R4 lots (roughly 240–290 m²) that genuinely can't fit Part F's setbacks raise an uncaught exception (surfaces as an HTTP 500) rather than a clean message.
- Tiered rules (e.g. site coverage % by lot area) are returned as all tiers; the caller must determine which applies — lot area isn't fed into the rule-selection logic yet.
- LMR (`/lmr`) and CDC (`/cdc-eligibility`) are preliminary screens, not determinations — several statutory exclusions (bushfire, flood, TOD overlap, ANEF noise contours, etc.) aren't verified.
- The extraction pipeline only covers DCP documents; LEP, both Codes SEPP 2008 codes, Housing SEPP 2021 Ch6, and the ADG are hand-maintained in the rule JSON and won't be touched by re-running `pipeline.py`.
- `pipeline.py`'s schema-invalid ("flagged") rules aren't excluded from its output file — currently zero instances in live data, but a live risk on the next extraction run.

**Dependencies:**
- **External, no-key-required:** NSW ArcGIS Geocoder, NSW Six Maps Cadastre, NSW Planning spatial layers (zone/LGA/FSR/height/lot-size/heritage), NSW Transport road-segment layer, NSW SEPP_Housing_2021 Town Centres layer.
- **Optional, extraction-only:** `GROQ_API_KEY` or `GEMINI_API_KEY` (`.env`) — only needed to re-run `pipeline.py`; not required to run the backend/frontend against already-extracted rules.
- **Backend:** FastAPI, Shapely, PyMuPDF, pandas, requests.
- **Frontend:** React, Leaflet/react-leaflet, Vite.
- **Plugin:** Rhino/Grasshopper (Python component), requires the backend running and reachable at `http://localhost:8000` (hardcoded, not currently configurable from the plugin side).

---

## 9. How to Use It

### Backend
```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```
No API keys needed to run against the already-extracted rules in `data/`.

### Frontend (map UI)
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:5173`, enter a Canada Bay address (e.g. `35 Connecticut Avenue Five Dock 2046`), and view the lot, computed envelope, and cited rules.

### Grasshopper plugin
1. Start the backend on the default port 8000 (the plugin calls `http://localhost:8000` directly, hardcoded).
2. Open `RhinoPlugInV1.gh` in Rhino.
3. Feed a Canada Bay address into input `x`.
4. Toggle `y` to run.
5. Outputs: lot curve, envelope curve, 3D mass, plus citation/LMR/control/label data — all computed by the backend, not recalculated in Grasshopper (§6.1).

### Rule extraction (only needed to add DCP coverage or re-extract)
```bash
python pipeline.py                          # all LGAs in pipeline_manifest.json (currently: Canada Bay only)
python pipeline.py --lga "Canada Bay R3"     # one entry
```

### Tests
```bash
python test_accuracy.py                     # rule-extraction accuracy vs ground truth (100%/100% currently)
python test_accuracy.py --skip-geometry     # skip the envelope-geometry check added in this engagement
```

---

## 10. Metrics (Current, Verified — Not the Original Projections)

| Metric | Original doc claimed | Actually verified now |
|---|---|---|
| Councils live | 2 (Canada Bay + Inner West) | **1** (Canada Bay only — Inner West deliberately removed) |
| Total rules loaded | ~240 | **~1,096** (482 R2 + 572 R3 + 17 Part G + 25 SEPP Ch6) |
| Rule-extraction accuracy | 95% | **100%** recall & precision against 26 verified ground-truth addresses (Canada Bay R2/R3/R4) |
| Envelope geometry — computes successfully | not measured | **66/70** cached lots (4 failures are lots genuinely too small for required setbacks) |
| Envelope geometry — stays within lot boundary | not measured | **66/66** |
| Envelope geometry — setback distance correct (±0.3m/10%) | "±0.1%" claimed | **116/132** — the 16 failures are the known wide/shallow-lot issue (§6.8), not new |
| Extraction model | "Claude-powered" | **Gemini/Groq** (Anthropic removed) |

---

## 11. Risks (Updated)

1. **NSW API downtime** — mitigated by aggressive caching; no offline mode exists yet.
2. **Council/state policy changes** — the Codes SEPP 2008 version stamp in `main.py` shows it's already been amended once since this data was extracted; there's no automated re-extraction trigger.
3. **The wide/shallow-lot setback-swap bug (§6.8)** — real, confirmed, unfixed. This is the single most important open item for anyone relying on this tool's numbers today.
4. **Legal liability** — a wrong envelope number presented confidently is the worst failure mode for a compliance tool; both disclaimers added in this engagement (§6.9, §6.8) exist specifically to reduce this risk by disclosing uncertainty rather than hiding it.
5. **Domain-expertise gap** — the root reason for the scope pivot (§5): neither founder has a formal architecture/planning background, which made it genuinely hard to audit a fast-expanding multi-council system for correctness. This is a standing risk for any future expansion, not just a historical note — the same gap will resurface the moment the project grows again, unless closed by bringing in domain review (an architect/planner sign-off step) alongside any technical scaling.
6. **Scope-vs-correctness trade-off** — the roadmap's original growth targets (10 councils by Q3, 50 by 2026) assumed the underlying engine was already solid; it wasn't. Any future roadmap should gate "add more councils" behind "the wide/shallow-lot issue is understood and fixed," not run in parallel with it.

---

## 12. Recommendation Going Forward

Before repeating the original multi-council growth roadmap, two things should happen, not one:

1. **Resolve the open geometry question in §6.8** — is edge-detection reliable enough to trust, and under what lot-shape conditions does it fail — with real investigation rather than a recalibrated threshold. Scaling to more councils multiplies whatever the geometry engine gets wrong.
2. **Close the domain-expertise gap that caused the pivot in the first place** (§5, §11.5). The team correctly recognised they couldn't audit their own output without architectural/planning expertise and scaled down in response — but scaling back up will reintroduce the exact same blind spot unless it's addressed directly, e.g. by bringing in an architect or planner to review outputs before expanding coverage again, not just by writing more tests.

The 5 July decision to stop, descope, and get one council right before going wider was the correct instinct. The same discipline — and this time, the domain review to back it up — should gate the next expansion decision too.
