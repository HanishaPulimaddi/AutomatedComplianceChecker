"""
extract_rules_iw.py — Extract structured rules from Inner West DCP chunks.

Runs Claude on each Inner West chunk file and writes one rules JSON per LGA
(R2 Low Density Residential suburbs only):
  data/rules_inner_west_marrickville.json
  data/rules_inner_west_ashfield.json

Extended parameter vocab vs Canada Bay extractor:
  - parking_spaces_per_dwelling, min_bicycle_spaces, max_driveway_width
  - front_fence_height_solid, front_fence_height_open
  - side_fence_height, rear_fence_height
  - Handles Ashfield DS\d+.\d+ clause labels (design solutions)

Usage:
    cd extractor/
    python extract_rules_iw.py
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

import sys
# Force UTF-8 output on Windows so arrow characters don't crash
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR  = BASE_DIR / "data"

SYSTEM_PROMPT = """You are a planning compliance expert reading Australian Development Control Plans (DCPs) from Inner West Council (Sydney).

Your job: extract structured rules from a single chunk of DCP text.

## RULES FOR EXTRACTION

1. Only extract rules that have a CLEAR NUMERIC VALUE (metres, percentages, storeys, ratios, car spaces, mm)
2. One chunk may contain 0, 1, or multiple rules
3. If no numeric rule is present, return {"rules": []}
4. Return valid JSON only. No prose, no explanation, no markdown.
5. Extract from CONTROLS (C1, C2...) and DESIGN SOLUTIONS (DS1.1, DS3.4...) — NOT from Objectives (O1, O2...)

## RULE SCHEMA

Each rule must have ALL of these fields:

- parameter: MUST be one of:
  [
    "front_setback", "side_setback_ground", "side_setback_upper",
    "rear_setback", "rear_setback_upper",
    "max_height", "max_storeys", "height_plane", "max_wall_height",
    "landscaped_area_pct", "landscaped_area_front_pct",
    "private_open_space", "private_open_space_min_dimension",
    "fsr", "site_coverage_pct",
    "min_lot_size", "min_lot_width", "min_dwelling_width",
    "basement_setback", "outbuilding_setback",
    "front_fence_height_solid", "front_fence_height_open",
    "side_fence_height", "rear_fence_height",
    "parking_spaces_per_dwelling", "min_bicycle_spaces",
    "max_driveway_width", "min_parking_space_length", "min_parking_space_width",
    "balcony_rear_setback", "building_separation"
  ]

- value: the numeric value (number, not string)
- unit: one of ["m", "m2", "mm", "pct", "storeys", "ratio", "degrees", "spaces"]
- operator: one of ["min", "max", "eq"]
  - "min" = the value is a minimum (e.g. "at least 4.5m")
  - "max" = the value is a maximum (e.g. "must not exceed 8.5m")
  - "eq" = exact value

- zone: which zone this applies to. Use "R2" for Low Density Residential.
  Use "all_residential" if applies to all residential zones.
  Use "all" if it applies across all zones (common for parking and fencing sections).

- dwelling_type: MUST be one of:
  ["dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
   "semi_detached", "secondary_dwelling", "outbuilding", "all"]

  CRITICAL dwelling_type rules:
  - Use "dwelling_house" when the rule applies to dwelling houses (even if the text also mentions secondary dwellings in parentheses, e.g. "Dwelling houses (incl. attached, semi-detached and secondary dwellings)")
  - Use "secondary_dwelling" ONLY when the rule EXCLUSIVELY applies to secondary dwellings and NOT to the principal/main dwelling
  - Use "all" for parking/fencing rules that apply to multiple dwelling types
  - In parking tables: the row "Dwelling houses (incl. ... secondary dwellings)" = dwelling_type="dwelling_house". Do NOT extract this row multiple times — extract it ONCE as dwelling_house.
  - Do NOT create a separate secondary_dwelling rule from the same table cell that already produced a dwelling_house rule.

- storey_applicability: one of:
  ["single_storey", "second_storey", "all_storeys", "not_specified"]

- lot_type: one of:
  ["single_frontage", "corner_lot", "internal_lot", "all", "not_specified"]

- conditions: list of strings — WHEN this rule applies (copy language from DCP text).
  Examples:
  - "primary street frontage only"
  - "Parking Area 1 (inner area)"
  - "lots less than 300sqm"
  - "row housing or terraces"

- exceptions: list of strings — WHEN this rule does NOT apply.
  Examples:
  - "does not apply to heritage conservation areas"
  - "Council may waive if within 400m of train station"
  - "not required for secondary dwellings"

- related_rules: list of strings referencing other clauses or documents.

- source_clause: the clause identifier (e.g. "C6", "DS3.4", "C18")
- source_text_quote: exact quote from the chunk (15-25 words) containing the numeric value
- confidence: 0.0 to 1.0
  - 0.9+ = clear numeric value, unambiguous parameter
  - 0.7-0.9 = some interpretation needed
  - below 0.7 = uncertain

## KEY EXTRACTION RULES

- ONE clause with DIFFERENT values per dwelling type = separate rules per type
- Tables (parking rates table, site coverage table) = one rule per row/threshold
- "minimum of X or Y whichever is greater" → value=X, put Y in conditions[]
- Convert mm to m: 900mm = 0.9m, 1500mm = 1.5m
- For parking: "1 space per dwelling" = value=1, unit="spaces", parameter="parking_spaces_per_dwelling"
- For fencing: front fence = front_fence_height_*, side fence = side_fence_height, rear = rear_fence_height
- Ashfield DS labels: DS3.1 = design solution, treat same as a Control
- Do NOT extract qualitative rules without numbers
- Do NOT invent numbers not in the text
- Do NOT extract site-specific rules that name a specific street address or site (e.g. "67 Smith Street", "Item 3 on Diagram 1") — these only apply to one property
- Do NOT extract rules about neighbouring/adjoining properties (e.g. solar access to neighbour's windows) — only extract rules that constrain the development being assessed
- For parking tables with many land uses: ONLY extract the "Dwelling houses" row. Skip commercial, retail, industrial, recreation rows entirely.

## RETURN FORMAT

{
  "rules": [
    {
      "parameter": "front_setback",
      "value": 4.5,
      "unit": "m",
      "operator": "min",
      "zone": "R2",
      "dwelling_type": "dwelling_house",
      "storey_applicability": "all_storeys",
      "lot_type": "single_frontage",
      "conditions": [],
      "exceptions": [],
      "related_rules": [],
      "source_clause": "C10",
      "source_text_quote": "Front setback must be consistent with the setbacks of adjoining properties, minimum 4.5m",
      "confidence": 0.92
    }
  ]
}
"""


def extract_rules_from_chunk(chunk: dict, lga: str) -> list[dict]:
    user_message = f"""Chunk from: {chunk['source_document']}
Section: {chunk['section']} ({chunk.get('section_title', '')})
Page: {chunk['page']}
Clause type: {chunk.get('clause_type', 'unknown')}

--- CHUNK TEXT ---
{chunk['text']}
--- END CHUNK ---

Extract all numeric rules from this chunk. Return JSON only."""

    for attempt in range(4):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            break
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                time.sleep(15 * (attempt + 1))  # 15s, 30s, 45s backoff
            else:
                raise

    raw = response.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1][4:] if parts[1].startswith("json") else parts[1]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
        rules = parsed.get("rules", [])
    except json.JSONDecodeError as e:
        print(f"\n  WARNING: JSON parse error on {chunk['chunk_id']}: {e}")
        print(f"  Raw: {raw[:200]}")
        return []

    enriched = []
    for i, rule in enumerate(rules):
        enriched.append({
            "rule_id":              f"{chunk['chunk_id']}_r{i+1}",
            "parameter":            rule.get("parameter"),
            "zone":                 rule.get("zone", "R2"),
            "lga":                  lga,
            "value":                rule.get("value"),
            "unit":                 rule.get("unit"),
            "operator":             rule.get("operator"),
            "dwelling_type":        rule.get("dwelling_type", "all"),
            "storey_applicability": rule.get("storey_applicability", "not_specified"),
            "lot_type":             rule.get("lot_type", "not_specified"),
            "conditions":           rule.get("conditions", []),
            "exceptions":           rule.get("exceptions", []),
            "related_rules":        rule.get("related_rules", []),
            "source_document":      chunk["source_document"],
            "source_clause":        f"{chunk['section']} {rule.get('source_clause', '')}".strip(),
            "source_page":          chunk["page"],
            "source_text":          rule.get("source_text_quote", chunk["text"][:150]),
            "source_type":          "dcp_extraction",
            "superseded_by":        None,
            "verified":             False,
            "extracted_by":         MODEL,
            "confidence":           rule.get("confidence", 0.5),
        })

    return enriched


def deduplicate(rules: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in rules:
        key = (r.get("parameter"), r.get("value"), r.get("unit"),
               r.get("dwelling_type"), r.get("storey_applicability"),
               r.get("lot_type"), r.get("zone"))
        if key not in seen:
            seen.add(key)
            out.append(r)
        else:
            print(f"  Dedup: {r['parameter']}={r['value']}{r['unit']} "
                  f"({r['dwelling_type']}/{r['storey_applicability']})")
    return out


VALID_PARAMETERS = {
    "front_setback", "side_setback_ground", "side_setback_upper",
    "rear_setback", "rear_setback_upper", "max_height", "max_storeys",
    "height_plane", "max_wall_height", "landscaped_area_pct",
    "landscaped_area_front_pct", "private_open_space",
    "private_open_space_min_dimension", "fsr", "site_coverage_pct",
    "min_lot_size", "min_lot_width", "min_dwelling_width",
    "basement_setback", "outbuilding_setback",
    "front_fence_height_solid", "front_fence_height_open",
    "side_fence_height", "rear_fence_height",
    "parking_spaces_per_dwelling", "min_bicycle_spaces",
    "max_driveway_width", "min_parking_space_length", "min_parking_space_width",
    "balcony_rear_setback", "building_separation",
}

VALID_UNITS     = {"m", "m2", "mm", "pct", "storeys", "ratio", "degrees", "spaces"}
VALID_OPERATORS = {"min", "max", "eq"}
VALID_DW_TYPES  = {
    "dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
    "semi_detached", "attached_dwelling", "secondary_dwelling", "outbuilding", "all",
}


def validate(rule: dict) -> list[str]:
    warn = []
    if rule.get("parameter") not in VALID_PARAMETERS:
        warn.append(f"invalid parameter: {rule.get('parameter')}")
    if rule.get("unit") not in VALID_UNITS:
        warn.append(f"invalid unit: {rule.get('unit')}")
    if rule.get("operator") not in VALID_OPERATORS:
        warn.append(f"invalid operator: {rule.get('operator')}")
    if rule.get("dwelling_type") not in VALID_DW_TYPES:
        warn.append(f"invalid dwelling_type: {rule.get('dwelling_type')}")
    if not isinstance(rule.get("value"), (int, float)):
        warn.append(f"value not a number: {rule.get('value')}")
    return warn


# ── Per-file config ──────────────────────────────────────────────────────────

TARGETS = [
    {
        "chunks":  DATA_DIR / "chunks_inner_west_marrickville.jsonl",
        "output":  DATA_DIR / "rules_inner_west_marrickville.json",
        "lga":     "Inner West Council - Marrickville",
        "label":   "Marrickville",
    },
    {
        "chunks":  DATA_DIR / "chunks_ashfield_dcp_f.jsonl",
        "output":  DATA_DIR / "rules_inner_west_ashfield.json",
        "lga":     "Inner West Council - Ashfield",
        "label":   "Ashfield",
    },
]
# Former Leichhardt LGA (R1 General Residential) excluded — outside R2 scope.


def process_target(target: dict):
    label   = target["label"]
    lga     = target["lga"]
    chunks_path = target["chunks"]
    out_path    = target["output"]

    print(f"\n{'='*60}")
    print(f"Processing: {label}")
    print(f"Input : {chunks_path.name}")
    print(f"Output: {out_path.name}")

    # Load chunks
    chunks = [json.loads(l) for l in open(chunks_path) if l.strip()]
    print(f"Loaded {len(chunks)} chunks")

    # Only process control-type chunks (skip headings, objectives, body)
    processable = [c for c in chunks
                   if c.get("clause_type") in ("control", "body")
                   and c.get("measurements")]   # only chunks that have numbers
    skipped = len(chunks) - len(processable)
    print(f"Processing {len(processable)} chunks with measurements "
          f"(skipping {skipped} without numbers)")
    print(f"Estimated API calls: {len(processable)}")
    print("-" * 60)

    all_rules = []
    errors = 0
    completed = 0

    # Run up to 8 API calls in parallel — cuts wall time ~8x
    def _call(chunk):
        return chunk, extract_rules_from_chunk(chunk, lga)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_call, c): c for c in processable}
        for future in as_completed(futures):
            completed += 1
            try:
                chunk, rules = future.result()
                all_rules.extend(rules)
                summary = (", ".join(f"{r['parameter']}={r['value']}{r['unit']}"
                                     for r in rules) if rules else "0 rules")
                print(f"  [{completed}/{len(processable)}] {chunk['chunk_id']} -> {summary}",
                      flush=True)
            except Exception as e:
                errors += 1
                print(f"  ERROR: {e}", flush=True)

    print(f"\n  Raw rules extracted: {len(all_rules)}, errors: {errors}")

    # Dedup
    unique = deduplicate(all_rules)
    print(f"  After dedup: {len(unique)}")

    # Validate
    valid, flagged = [], []
    for r in unique:
        w = validate(r)
        if w:
            r["validation_warnings"] = w
            flagged.append(r)
            print(f"  FLAG {r['rule_id']}: {w}")
        else:
            valid.append(r)

    print(f"  Valid: {len(valid)}, flagged: {len(flagged)}")

    # Save all (valid + flagged) — backend filters by confidence >= 0.8
    all_out = valid + flagged
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_out, f, indent=2, ensure_ascii=False)
    print(f"  Saved {len(all_out)} rules -> {out_path.name}")

    # Summary by parameter
    by_param = {}
    for r in valid:
        by_param.setdefault(r["parameter"], []).append(r)

    print(f"\n  Parameter breakdown ({len(by_param)} unique parameters):")
    for param in sorted(by_param):
        rs = by_param[param]
        print(f"    {param}: {len(rs)} rule(s) — "
              + ", ".join(f"{r['value']}{r['unit']}" for r in rs[:4])
              + ("..." if len(rs) > 4 else ""))

    return all_out


def main():
    for target in TARGETS:
        process_target(target)

    print("\n" + "="*60)
    print("Done. Output files:")
    for t in TARGETS:
        n = len(json.load(open(t["output"])))
        print(f"  {t['output'].name}: {n} rules")


if __name__ == "__main__":
    main()
