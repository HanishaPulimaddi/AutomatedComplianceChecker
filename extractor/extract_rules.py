"""
extract_rules.py — Use Claude to extract structured rules from DCP chunks.
Reads chunks_canada_bay_dcp_part_e.jsonl, calls Claude per chunk,
writes rules_r2_canada_bay_raw.json.

Usage:
    cd extractor/
    python extract_rules.py
"""

import sys
sys.path.insert(0, "D:/python_packages")

import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from google import genai as _genai
_gemini = _genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"


def _call_gemini(system_prompt: str, user_content: str, max_tokens: int = 2000) -> str:
    for attempt in range(4):
        try:
            resp = _gemini.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"{system_prompt}\n\n{user_content}",
                config={"max_output_tokens": max_tokens, "temperature": 0},
            )
            return resp.text.strip()
        except Exception as e:
            if ("429" in str(e) or "quota" in str(e).lower()) and attempt < 3:
                time.sleep(15 * (attempt + 1))
            else:
                raise
    raise RuntimeError("Gemini failed after 4 attempts")

SYSTEM_PROMPT = """You are a planning compliance expert reading an Australian Development Control Plan (DCP).

Your job: extract structured rules from a single chunk of DCP text.

## RULES FOR EXTRACTION

1. Only extract rules that have a CLEAR NUMERIC VALUE (metres, percentages, storeys, ratios, degrees, square metres)
2. One chunk may contain 0, 1, or multiple rules
3. If no numeric rule is present, return {"rules": []}
4. Return valid JSON only. No prose, no explanation, no markdown.

## RULE SCHEMA

Each rule must have ALL of these fields:

- parameter: MUST be one of:
  ["front_setback", "side_setback_ground", "side_setback_upper", "rear_setback",
   "rear_setback_upper", "max_height", "max_storeys", "height_plane",
   "landscaped_area_pct", "landscaped_area_front_pct", "landscaped_area_rear_pct",
   "private_open_space", "private_open_space_min_dimension",
   "fsr", "min_lot_size", "min_dwelling_width",
   "basement_setback", "outbuilding_setback",
   "front_fence_height_solid", "front_fence_height_open",
   "balcony_rear_setback", "balcony_side_setback",
   "building_separation"]

- value: the numeric value (number, not string)
- unit: one of ["m", "m2", "mm", "pct", "storeys", "ratio", "degrees", "dBA"]
- operator: one of ["min", "max", "eq"]
  - "min" = the value is a minimum (e.g. "at least 4.5m")
  - "max" = the value is a maximum (e.g. "must not exceed 8.5m")
  - "eq" = the value is exact (e.g. "shall be 45 degrees")

- zone: which zone this applies to. Usually "R2" for single dwellings. Use "all_residential" if not zone-specific.

- dwelling_type: who this rule applies to. MUST be one of:
  ["dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
   "semi_detached", "secondary_dwelling", "outbuilding", "all"]
  Use "all" if the rule applies to all dwelling types in Part E.

- storey_applicability: which storeys this rule applies to. One of:
  ["single_storey", "second_storey", "all_storeys", "not_specified"]
  Example: "900mm side setback" applies to "single_storey",
  "1500mm side setback" applies to "second_storey"

- lot_type: what lot configuration this applies to. One of:
  ["single_frontage", "corner_lot", "parallel_road", "internal_lot", "all", "not_specified"]

- conditions: list of strings describing WHEN this rule applies.
  Examples:
  - "primary street frontage"
  - "or prevailing street setback whichever is greater"
  - "within first 25m from corner"
  - "if rear dwelling faces secondary frontage"
  - "within biodiversity corridor"
  Be specific. Copy the condition language from the DCP text.

- exceptions: list of strings describing WHEN this rule does NOT apply.
  Examples:
  - "does not apply to outbuildings"
  - "does not apply to secondary dwellings"
  - "eaves and awnings for weather protection are exempt"
  - "may be reduced if no maintenance required and no adverse impacts"
  If no exceptions mentioned, use []

- related_rules: list of strings referencing other clauses or documents this rule depends on.
  Examples:
  - "see Canada Bay LEP Height of Buildings Map"
  - "refer to Part B for tree controls"
  - "subject to Foreshore Building Line"
  If none mentioned, use []

- source_clause: the clause identifier (e.g. "C1", "C6", "C9")
- source_text_quote: exact quote from the chunk text (15-25 words max) that contains the numeric value
- confidence: 0.0 to 1.0
  - 0.9+ = clear numeric value with obvious parameter mapping
  - 0.7-0.9 = numeric value present but some interpretation needed
  - below 0.7 = uncertain, might be misreading the rule

## IMPORTANT EXTRACTION GUIDELINES

- If ONE clause contains DIFFERENT rules for different dwelling types, extract EACH as a separate rule
  Example: "Single storey dwellings: 900mm. Second storey: 1500mm" = TWO rules, not one.

- If a rule says "minimum of X or Y, whichever is greater" extract value as X and put the Y condition in conditions[]

- Tables with different values per dwelling type = one rule per row

- Do NOT extract objectives (O1, O2...) — only extract controls (C1, C2...)

- Do NOT extract qualitative rules without numbers (e.g. "should complement the streetscape")

- Do NOT invent numbers that are not in the text

- Pay attention to UNITS: 900mm = 0.9m, 1500mm = 1.5m. Always convert to metres for setbacks.

- For landscaped area percentages, extract separately for front setback, rear setback, and total site area if different values are given.

- For private open space, extract both the area (m2) and minimum dimension (m) as separate rules.

## RETURN FORMAT

{
  "rules": [
    {
      "parameter": "side_setback_ground",
      "value": 0.9,
      "unit": "m",
      "operator": "min",
      "zone": "R2",
      "dwelling_type": "dwelling_house",
      "storey_applicability": "single_storey",
      "lot_type": "single_frontage",
      "conditions": [],
      "exceptions": [],
      "related_rules": [],
      "source_clause": "C6",
      "source_text_quote": "Single storey dwellings are to be set back a minimum of 900mm from side boundaries",
      "confidence": 0.95
    },
    {
      "parameter": "side_setback_upper",
      "value": 1.5,
      "unit": "m",
      "operator": "min",
      "zone": "R2",
      "dwelling_type": "dwelling_house",
      "storey_applicability": "second_storey",
      "lot_type": "single_frontage",
      "conditions": [],
      "exceptions": [],
      "related_rules": [],
      "source_clause": "C6",
      "source_text_quote": "second storey of all dwellings are to be set back a minimum of 1500mm from side boundaries",
      "confidence": 0.95
    }
  ]
}
"""


def extract_rules_from_chunk(chunk: dict) -> list[dict]:
    """Call Claude on a single chunk, return list of rule dicts."""

    user_message = f"""Chunk from: {chunk['source_document']}
Section: {chunk['section']} ({chunk.get('section_title', 'N/A')})
Page: {chunk['page']}
Clause type: {chunk.get('clause_type', 'unknown')}

--- CHUNK TEXT ---
{chunk['text']}
--- END CHUNK ---

Extract all numeric rules from this chunk. Return JSON only."""

    raw_text = _call_gemini(SYSTEM_PROMPT, user_message, max_tokens=2000)

    # Strip markdown code fences if Claude added them
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        if len(parts) >= 2:
            raw_text = parts[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
        rules = parsed.get("rules", [])
    except json.JSONDecodeError as e:
        print(f"\n  ⚠ JSON parse error on {chunk['chunk_id']}: {e}")
        print(f"  Raw response: {raw_text[:200]}...")
        return []

    # Enrich each rule with chunk metadata
    enriched = []
    for i, rule in enumerate(rules):
        rule_id = f"{chunk['chunk_id']}_r{i+1}"
        enriched.append({
            # Identity
            "rule_id": rule_id,

            # The rule itself
            "parameter": rule.get("parameter"),
            "zone": rule.get("zone", "R2"),
            "lga": "City of Canada Bay",
            "value": rule.get("value"),
            "unit": rule.get("unit"),
            "operator": rule.get("operator"),

            # Applicability
            "dwelling_type": rule.get("dwelling_type", "all"),
            "storey_applicability": rule.get("storey_applicability", "not_specified"),
            "lot_type": rule.get("lot_type", "not_specified"),

            # Conditions and exceptions
            "conditions": rule.get("conditions", []),
            "exceptions": rule.get("exceptions", []),
            "related_rules": rule.get("related_rules", []),

            # Source traceability
            "source_document": chunk["source_document"],
            "source_clause": f"{chunk['section']} {rule.get('source_clause', '')}".strip(),
            "source_page": chunk["page"],
            "source_text": rule.get("source_text_quote", chunk["text"][:150]),
            "source_type": "dcp_extraction",

            # Verification
            "superseded_by": None,
            "verified": False,
            "extracted_by": MODEL,
            "confidence": rule.get("confidence", 0.5),
        })

    return enriched


def deduplicate_rules(rules: list[dict]) -> list[dict]:
    """Remove duplicate rules (same parameter + value + dwelling_type + storey)."""
    seen = set()
    unique = []
    for rule in rules:
        key = (
            rule.get("parameter"),
            rule.get("value"),
            rule.get("unit"),
            rule.get("dwelling_type"),
            rule.get("storey_applicability"),
            rule.get("lot_type"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(rule)
        else:
            print(f"  Dedup: skipping duplicate {rule['parameter']}={rule['value']}{rule['unit']} "
                  f"({rule['dwelling_type']}/{rule['storey_applicability']})")
    return unique


def validate_rule(rule: dict) -> list[str]:
    """Check a rule for common issues. Returns list of warnings."""
    warnings = []

    valid_parameters = [
        "front_setback", "side_setback_ground", "side_setback_upper",
        "rear_setback", "rear_setback_upper", "max_height", "max_storeys",
        "height_plane", "landscaped_area_pct", "landscaped_area_front_pct",
        "landscaped_area_rear_pct", "private_open_space",
        "private_open_space_min_dimension", "fsr", "min_lot_size",
        "min_dwelling_width", "basement_setback", "outbuilding_setback",
        "front_fence_height_solid", "front_fence_height_open",
        "balcony_rear_setback", "balcony_side_setback", "building_separation",
    ]

    valid_units = ["m", "m2", "mm", "pct", "storeys", "ratio", "degrees", "dBA"]
    valid_operators = ["min", "max", "eq"]
    valid_dwelling_types = [
        "dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
        "semi_detached", "secondary_dwelling", "outbuilding", "all",
    ]

    if rule.get("parameter") not in valid_parameters:
        warnings.append(f"Invalid parameter: {rule.get('parameter')}")

    if rule.get("unit") not in valid_units:
        warnings.append(f"Invalid unit: {rule.get('unit')}")

    if rule.get("operator") not in valid_operators:
        warnings.append(f"Invalid operator: {rule.get('operator')}")

    if rule.get("dwelling_type") not in valid_dwelling_types:
        warnings.append(f"Invalid dwelling_type: {rule.get('dwelling_type')}")

    if rule.get("value") is None:
        warnings.append("Missing value")

    if not isinstance(rule.get("value"), (int, float)):
        warnings.append(f"Value is not a number: {rule.get('value')}")

    return warnings


def main():
    chunks_path = "../data/chunks_canada_bay_dcp_part_e.jsonl"
    output_path = "../data/rules_r2_canada_bay_raw.json"
    validated_output_path = "../data/rules_r2_canada_bay_validated.json"

    # ── Load chunks ──
    chunks = []
    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))

    print(f"Loaded {len(chunks)} chunks from {chunks_path}")
    print(f"Using model: {MODEL}")
    print(f"Estimated cost: ~${len(chunks) * 0.005:.2f}")
    print("-" * 60)

    # ── Extract rules from each chunk ──
    all_rules = []
    errors = 0
    empty_chunks = 0

    for i, chunk in enumerate(chunks, 1):
        section_info = f"{chunk['section']}"
        if chunk.get('section_title'):
            section_info += f" - {chunk['section_title']}"

        print(f"[{i}/{len(chunks)}] {chunk['chunk_id']} ({section_info})...", end=" ")

        # Skip objective-only chunks (no numeric rules)
        if chunk.get("clause_type") == "objective":
            print("→ skipped (objective)")
            empty_chunks += 1
            continue

        try:
            rules = extract_rules_from_chunk(chunk)
            all_rules.extend(rules)

            if len(rules) == 0:
                print("→ 0 rules")
                empty_chunks += 1
            else:
                rule_summary = ", ".join(
                    f"{r['parameter']}={r['value']}{r['unit']}"
                    for r in rules
                )
                print(f"→ {len(rules)} rule(s): {rule_summary}")

            # Small delay to avoid rate limiting
            time.sleep(0.5)

        except Exception as e:
            print(f"⚠ error: {e}")
            errors += 1

    print("-" * 60)
    print(f"Extraction complete.")
    print(f"  Total chunks processed: {len(chunks)}")
    print(f"  Chunks with rules: {len(chunks) - empty_chunks - errors}")
    print(f"  Chunks without rules: {empty_chunks}")
    print(f"  Errors: {errors}")
    print(f"  Total raw rules: {len(all_rules)}")

    # ── Deduplicate ──
    print(f"\nDeduplicating...")
    unique_rules = deduplicate_rules(all_rules)
    print(f"  Before dedup: {len(all_rules)}")
    print(f"  After dedup: {len(unique_rules)}")

    # ── Validate ──
    print(f"\nValidating...")
    valid_rules = []
    invalid_rules = []

    for rule in unique_rules:
        warnings = validate_rule(rule)
        if warnings:
            print(f"  ⚠ {rule['rule_id']}: {', '.join(warnings)}")
            rule["validation_warnings"] = warnings
            invalid_rules.append(rule)
        else:
            valid_rules.append(rule)

    print(f"  Valid rules: {len(valid_rules)}")
    print(f"  Rules with warnings: {len(invalid_rules)}")

    # ── Save raw output (everything) ──
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(unique_rules, f, indent=2, ensure_ascii=False)
    print(f"\nSaved all rules to {output_path}")

    # ── Save validated output (only clean rules) ──
    with open(validated_output_path, "w", encoding="utf-8") as f:
        json.dump(valid_rules, f, indent=2, ensure_ascii=False)
    print(f"Saved validated rules to {validated_output_path}")

    # ── Summary by parameter ──
    print("\n" + "=" * 60)
    print("SUMMARY BY PARAMETER")
    print("=" * 60)

    by_param = {}
    for r in valid_rules:
        p = r["parameter"]
        if p not in by_param:
            by_param[p] = []
        by_param[p].append(r)

    for param in sorted(by_param.keys()):
        rules = by_param[param]
        print(f"\n  {param} ({len(rules)} rule(s)):")
        for r in rules:
            dwelling = r.get("dwelling_type", "all")
            storey = r.get("storey_applicability", "n/a")
            lot = r.get("lot_type", "n/a")
            conf = r.get("confidence", 0)
            print(f"    → {r['value']}{r['unit']} ({r['operator']}) "
                  f"| {dwelling} | {storey} | {lot} "
                  f"| conf:{conf:.2f} | {r['source_clause']}")

    # ── Ground truth check ──
    print("\n" + "=" * 60)
    print("GROUND TRUTH QUICK CHECK")
    print("=" * 60)

    ground_truth = {
        "front_setback": {"value": 4.5, "unit": "m"},
        "side_setback_ground": {"value": 0.9, "unit": "m"},
        "side_setback_upper": {"value": 1.5, "unit": "m"},
        "rear_setback": {"value": 6.0, "unit": "m"},
        "max_storeys": {"value": 2, "unit": "storeys"},
        "height_plane": {"value": 45, "unit": "degrees"},
        "landscaped_area_pct": {"value": 35, "unit": "pct"},
        "private_open_space": {"value": 40, "unit": "m2"},
    }

    for param, expected in ground_truth.items():
        matching = [r for r in valid_rules
                    if r["parameter"] == param
                    and r.get("dwelling_type") in ("dwelling_house", "all")]

        if not matching:
            print(f"  {param}: NOT FOUND (expected {expected['value']}{expected['unit']})")
        else:
            found = matching[0]
            if found["value"] == expected["value"]:
                print(f"   {param}: {found['value']}{found['unit']} matches ground truth")
            else:
                print(f"   {param}: {found['value']}{found['unit']} ≠ expected {expected['value']}{expected['unit']}")


if __name__ == "__main__":
    main()