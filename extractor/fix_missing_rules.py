"""
fix_missing_rules.py — Re-extract specific chunks that failed.
"""

import json
import os
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-5-20250929"

SYSTEM_PROMPT = """You are a planning compliance expert. Extract all numeric rules from this DCP text.

Return JSON only in this format:
{
  "rules": [
    {
      "parameter": "landscaped_area_pct",
      "value": 35,
      "unit": "pct",
      "operator": "min",
      "zone": "R2",
      "dwelling_type": "dwelling_house",
      "storey_applicability": "not_specified",
      "lot_type": "all",
      "conditions": [],
      "exceptions": [],
      "related_rules": [],
      "source_clause": "C4",
      "source_text_quote": "exact quote from text",
      "confidence": 0.95
    }
  ]
}

Allowed parameters: landscaped_area_pct, landscaped_area_front_pct, landscaped_area_rear_pct
Allowed dwelling_types: dwelling_house, dual_occupancy_attached, dual_occupancy_detached, semi_detached, secondary_dwelling, outbuilding, all
"""


def main():
    # Load the specific chunk that failed
    chunks_path = "../data/chunks_canada_bay_dcp_part_e.jsonl"
    chunks = []
    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))

    # Find chunk 122 (the one that had the JSON parse error)
    target_chunk = None
    for c in chunks:
        if c["chunk_id"] == "cb_dcp_e_122":
            target_chunk = c
            break

    if not target_chunk:
        print("Chunk cb_dcp_e_122 not found. Searching for landscaped area chunks...")
        for c in chunks:
            if "landscaped" in c.get("section_title", "").lower() or "35%" in c.get("text", ""):
                print(f"  Found: {c['chunk_id']} - {c['section']} - {c['text'][:100]}...")
                target_chunk = c
                break

    if not target_chunk:
        print("No landscaped area chunk found!")
        return

    print(f"Re-extracting: {target_chunk['chunk_id']}")
    print(f"Text preview: {target_chunk['text'][:200]}...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,  # Higher limit to avoid truncation
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Extract all numeric landscaped area rules:\n\n{target_chunk['text']}"
        }],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
        rules = parsed.get("rules", [])
        print(f"\nExtracted {len(rules)} rules:")
        for r in rules:
            print(f"  {r['parameter']} = {r['value']}{r['unit']} ({r['dwelling_type']})")
        
        # Save to a patch file
        with open("../data/landscaped_area_patch.json", "w") as f:
            json.dump(rules, f, indent=2)
        print("\nSaved to ../data/landscaped_area_patch.json")
        print("You'll merge these into the main rules file in the next step.")
        
    except json.JSONDecodeError as e:
        print(f"Parse error: {e}")
        print(f"Raw response:\n{raw}")


if __name__ == "__main__":
    main()