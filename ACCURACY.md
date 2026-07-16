# Extraction Accuracy Report

Last updated: 2026-07-16
Scope: City of Canada Bay Council only, R2/R3/R4 residential zones — Inner West (Marrickville/Ashfield) support was deliberately removed on 5 July 2026; see `PROJECT_REPORT.md` §5.
Ground truth: 26 NSW Planning API–verified addresses, values cross-checked against source DCP/LEP PDFs (`data/ground_truth_addresses.json`)

Figures below are reproducible with `python test_accuracy.py`.

---

## Rule-extraction accuracy

| Zone | Rules Loaded | GT Addresses | Recall | Precision |
|---|---|---|---|---|
| Canada Bay (R2) | 482 | 15 | **100%** | **100%** |
| Canada Bay R3 | 572 | 3 | **100%** | **100%** |
| Canada Bay R4 | 572 | 8 | **100%** | **100%** |
| **Overall** | | **26** | **100%** | **100%** |

## Envelope geometry accuracy

Checked against 70 cached lots (`python test_accuracy.py`, without `--skip-geometry`):

| Check | Result |
|---|---|
| Computed successfully | 66/70 |
| Stays within lot boundary | 66/66 |
| Setback distance correct (±0.3m or 10%, front+rear) | 116/132 |

---

## Known failures (still open)

### 1. Envelope fails to compute on 4 small lots

`10 Marquet Street Rhodes`, `5 Bayswater Street Drummoyne`, `30 Waterview Street Five Dock`, `5 Wentworth Drive Liberty Grove` — genuinely too small for the required setbacks to leave any buildable footprint. Currently surfaces as an uncaught exception (HTTP 500) rather than a clean "no buildable envelope" response. See `PROJECT_REPORT.md` §8.

### 2. Setback distance wrong on 16/132 checks — wide/shallow lot rear/side swap

The front/rear/side edge-detection heuristic ("rear = the edge whose midpoint sits furthest from the front edge's midpoint") misidentifies rear vs. side setbacks on lots wider than roughly 1.73× their depth. Confirmed on real addresses, e.g. Rhodes and Concord West lots where a 0.9–1.5m side setback was applied where 6.0m rear was required, or vice versa.

This is the single most important open accuracy issue — see `PROJECT_REPORT.md` §6.8 for the investigation, including why a parallelism-based guard was tried and rejected (it false-positived on 89% of simple lots).

### 3. Tiered rules returned without lot-area resolution

Rules like `site_coverage_pct` are returned as all tiers (e.g. 40%, 45%, 50%, 55%, 60%) rather than the single tier that applies to a given lot — lot area isn't currently fed into the rule-selection logic. The caller must pick the applicable tier.

---

## Fixed since last report

- **Longitude/latitude projection bug** — setbacks were silently under-applied by up to ~17% on east/west-facing edges because degree-space math didn't account for `cos(latitude)`. Fixed by projecting to a local metric coordinate system before any offset/area math. See `PROJECT_REPORT.md` §6.3.
- **Complex/curved boundaries** — lots with 12+ boundary vertices (e.g. curved waterfront cadastral boundaries) previously had edge-detection pick an architecturally meaningless micro-segment as the "front edge." Any lot over 12 vertices now returns its envelope plus an explicit warning (`envelope_fallback_warnings`) instead of a silently wrong number.

---

Historical Inner West (Marrickville/Ashfield) accuracy figures from the multi-council period (85 Canada Bay rules at 75% recall, plus Marrickville/Ashfield breakdowns) have been removed from this report — that scope was deliberately dropped and is no longer loaded by the running app. See git history if they're needed for reference.
