"""
fix_missing_rules.py — Re-extract specific chunks that failed.
"""

import sys
sys.path.insert(0, "D:/python_packages")

import json
import os
import time
from dotenv import load_dotenv

load_dotenv()

from google import genai as _genai
_gemini = _genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"


def _call_gemini(system_prompt: str, user_content: str, max_tokens: int = 4000) -> str:
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

    raw = _call_gemini(SYSTEM_PROMPT, f"Extract all numeric landscaped area rules:\n\n{target_chunk['text']}")
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