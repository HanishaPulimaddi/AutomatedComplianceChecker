"""
test_accuracy.py — Test extracted rules against verified ground truth.

For each verified address in data/ground_truth_addresses.json, loads the
rules file for that LGA and checks:
  - Recall: what fraction of GT parameters are covered by extracted rules
  - Precision: of parameters covered, how many values match

Usage:
    python test_accuracy.py                          # test all LGAs
    python test_accuracy.py --lga Ashfield           # one LGA
    python test_accuracy.py --rules data/rules_inner_west_ashfield_pipeline.json --lga Ashfield
    python test_accuracy.py --verbose                # show per-param detail
"""

import json
import re
import sys
import argparse
from pathlib import Path
from collections import defaultdict

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
GT_PATH  = BASE_DIR / "data" / "ground_truth_addresses.json"

# Maps dcp_routing → rules file (current hand-tuned files)
RULES_FILES = {
    "Inner West DCP 2016 Chapter F": BASE_DIR / "data" / "rules_inner_west_ashfield.json",
    "Marrickville DCP 2011":         BASE_DIR / "data" / "rules_inner_west_marrickville.json",
    "canada_bay":                    BASE_DIR / "data" / "rules_r2_canada_bay.json",
}

# Pipeline output files (used when --pipeline flag is set)
PIPELINE_FILES = {
    "Inner West DCP 2016 Chapter F": BASE_DIR / "data" / "rules_inner_west_ashfield_pipeline.json",
    "Marrickville DCP 2011":         BASE_DIR / "data" / "rules_inner_west_marrickville_pipeline.json",
    "canada_bay":                    BASE_DIR / "data" / "rules_r2_canada_bay_pipeline.json",
}

# LGA label → dcp_routing key
LGA_ROUTING_MAP = {
    "Ashfield":     "Inner West DCP 2016 Chapter F",
    "Marrickville": "Marrickville DCP 2011",
    "Canada Bay":   "canada_bay",
}

YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[91m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def c(color, text): return f"{color}{text}{RESET}"


# ── Value parsing ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"(\d+\.?\d*)")

def parse_expected_value(raw: str):
    """
    Extract the primary numeric value(s) from a GT check string.
    Returns (value_or_list, unit) or (None, None) if qualitative/unparseable.

    Handles:
      "0.9m min (DS4.3)"                          → (0.9, "m")
      "8.5m (LEP cl 4.3)"                         → (8.5, "m")
      "60%; 55%; 50%; 45%; 40% (C13 tiers)"       → ([60,55,50,45,40], "pct")
      "0-300m² on merit; 60% (>300m²)..."         → ([60,55,50,45,40], "pct")
      "1 per dwelling (Parking Areas 1/2/3)"      → (1.0, "spaces")
      "verify via Planning Portal"                 → (None, None)
      "NOT IN DCP"                                 → (None, None)
    """
    raw = raw.strip()

    skip_phrases = [
        "verify via", "planning portal", "qualitative", "NOT IN",
        "consistent with", "match", "streetscape", "BLZ",
        "determined on merit", "no design solution",
    ]
    if any(p.lower() in raw.lower() for p in skip_phrases):
        return None, None

    # Detect unit from full string
    unit = ("pct" if "%" in raw
            else "storeys" if "storey" in raw.lower()
            else "degrees" if ("°" in raw or "deg" in raw.lower())
            else "spaces" if "space" in raw.lower() or "per dwelling" in raw.lower()
            else "m2" if ("m²" in raw or "m2" in raw.lower())
            else "m")

    # Tiered: semicolon-separated tiers each containing a %/m value
    # e.g. "60% (>300m²); 55% (350m²); 50% ..."
    # Also handles "40/45/50/55/60" slash-separated tiers
    semis = [s.strip() for s in raw.split(";") if s.strip()]
    if len(semis) >= 3:
        tier_vals = []
        for seg in semis:
            seg_clean = seg.split("(")[0].strip()
            if "merit" in seg_clean.lower() or "determined" in seg_clean.lower():
                continue  # skip "on merit" tiers
            nums = _NUM.findall(seg_clean)
            if nums:
                # For % tiers take only the first number in each segment
                tier_vals.append(float(nums[0]))
        if len(tier_vals) >= 2:
            return tier_vals, unit

    # Slash-separated pure numeric tiers ONLY if slashes are between same-unit values
    # e.g. "40/45/50%" but NOT "Parking Areas 1/2/3"
    slash_match = re.search(r"(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?){2,})\s*%", raw)
    if slash_match:
        vals = [float(v) for v in slash_match.group(1).split("/")]
        return vals, "pct"

    # Single value — take first number before the first parenthesis / em-dash
    clean = raw.split("(")[0].split("—")[0].split("–")[0]
    nums = _NUM.findall(clean)
    if not nums:
        return None, None

    return float(nums[0]), unit


def values_match(expected_val, expected_unit: str, rules: list[dict],
                 tolerance: float = 0.01) -> tuple[bool, list[dict]]:
    """Check if any extracted rule matches the expected value."""
    if expected_val is None:
        return None, []   # None = skip (qualitative / map-based)

    matching = []
    if isinstance(expected_val, list):
        # Tiered: check that all expected tiers are present
        found_vals = {r["value"] for r in rules}
        matched_tiers = [v for v in expected_val if any(abs(v - fv) <= tolerance for fv in found_vals)]
        if len(matched_tiers) == len(expected_val):
            matching = [r for r in rules if any(abs(r["value"] - v) <= tolerance for v in expected_val)]
            return True, matching
        return False, []
    else:
        for r in rules:
            if abs(float(r.get("value", 0)) - expected_val) <= tolerance:
                matching.append(r)
        return bool(matching), matching


# ── Rule lookup ───────────────────────────────────────────────────────────────

def get_rules_for_lga(routing_key: str, rules_map: dict) -> list[dict]:
    path = rules_map.get(routing_key)
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def filter_rules(rules: list[dict], param: str, zone: str = "R2") -> list[dict]:
    """Return rules matching this parameter that are applicable to R2 dwelling houses."""
    return [
        r for r in rules
        if r.get("parameter") == param
        and r.get("zone") in (zone, "all_residential", "all", "not_specified")
        and r.get("dwelling_type") in ("dwelling_house", "all")
        and not r.get("superseded_by")
        and r.get("confidence", 0) >= 0.6
    ]


# ── Per-address test ──────────────────────────────────────────────────────────

def test_address(entry: dict, rules: list[dict], verbose: bool = False) -> dict:
    """
    Returns:
      {
        "address": ...,
        "checks": {param: "MATCH"|"WRONG"|"MISSING"|"SKIP"},
        "recall": float,   # params found / params expected
        "precision": float # correct / found
      }
    """
    address = entry["address"]
    checks  = entry.get("check", {})
    zone    = entry.get("expected_zone", "R2")

    results = {}
    skipped = 0

    for param, gt_raw in checks.items():
        expected_val, expected_unit = parse_expected_value(str(gt_raw))

        if expected_val is None:
            results[param] = ("SKIP", None, [])
            skipped += 1
            continue

        matching_rules = filter_rules(rules, param, zone)
        hit, matched = values_match(expected_val, expected_unit, matching_rules)

        if hit is None:
            results[param] = ("SKIP", expected_val, [])
            skipped += 1
        elif hit:
            results[param] = ("MATCH", expected_val, matched)
        elif matching_rules:
            # Rules found but wrong values
            found_vals = [r["value"] for r in matching_rules]
            results[param] = ("WRONG", expected_val, matching_rules)
        else:
            results[param] = ("MISSING", expected_val, [])

    checkable = len(checks) - skipped
    if checkable == 0:
        return {"address": address, "checks": results,
                "recall": None, "precision": None, "skipped": skipped}

    found    = sum(1 for s, *_ in results.values() if s in ("MATCH", "WRONG"))
    correct  = sum(1 for s, *_ in results.values() if s == "MATCH")
    recall   = found / checkable
    precision = correct / found if found > 0 else 0.0

    if verbose:
        print(f"\n  {c(BOLD, address)}")
        for param, (status, exp_val, matched) in sorted(results.items()):
            if status == "SKIP":
                print(f"    {c(DIM, '○')} {param:35} {c(DIM,'SKIP (qualitative/map)')}")
            elif status == "MATCH":
                rpath = matched[0].get("routing_path","") if matched else ""
                print(f"    {c(GREEN, '✓')} {param:35} {c(GREEN, str(exp_val))}  ← {rpath[:60]}")
            elif status == "WRONG":
                found_v = [r['value'] for r in matched]
                print(f"    {c(YELLOW,'~')} {param:35} expected={exp_val}  got={found_v}")
            else:
                print(f"    {c(RED, '✗')} {param:35} {c(RED,'MISSING')}  expected={exp_val}")

    return {
        "address":   address,
        "checks":    results,
        "recall":    recall,
        "precision": precision,
        "skipped":   skipped,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lga",      default="", help="Filter to one LGA label")
    parser.add_argument("--rules",    default="", help="Override rules file path")
    parser.add_argument("--pipeline", action="store_true",
                        help="Use pipeline output files instead of hand-tuned files")
    parser.add_argument("--verbose",  action="store_true", help="Show per-param detail")
    args = parser.parse_args()

    rules_map = PIPELINE_FILES if args.pipeline else RULES_FILES

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))

    # Filter to verified addresses only
    verified = [e for e in gt if e.get("verified_via", "pending") != "pending"]
    if args.lga:
        lga_key = args.lga.strip().title()
        verified = [e for e in verified
                    if args.lga.lower() in e.get("lga","").lower()
                    or args.lga.lower() in e.get("dcp_routing","").lower()
                    or args.lga.lower() in (LGA_ROUTING_MAP.get(lga_key,"")).lower()]
    if not verified:
        print("No verified addresses found. Run verify_ground_truth.py first.")
        return

    print(c(BOLD, f"\nAccuracy Test — {len(verified)} verified addresses"))
    print(f"Rules source: {'pipeline outputs' if args.pipeline else 'hand-tuned files'}")
    print("="*65)

    # Group by routing key
    by_routing = defaultdict(list)
    for e in verified:
        routing = e.get("dcp_routing", "")
        # Normalize Canada Bay routing
        if "canada" in routing.lower() or e.get("lga","").lower() == "city of canada bay":
            routing = "canada_bay"
        by_routing[routing].append(e)

    # Override rules file if specified
    if args.rules:
        override_path = Path(args.rules)
        for key in list(by_routing.keys()):
            rules_map[key] = override_path

    grand_recall    = []
    grand_precision = []

    for routing_key, entries in sorted(by_routing.items()):
        rules = get_rules_for_lga(routing_key, rules_map)
        if not rules:
            print(f"\n{c(RED, 'No rules loaded')} for routing={routing_key}")
            continue

        label = next((k for k, v in LGA_ROUTING_MAP.items()
                      if v == routing_key), routing_key)
        print(f"\n{c(BOLD, label)}  ({len(rules)} rules, {len(entries)} addresses)")
        print("-"*55)

        recalls, precisions = [], []
        missing_params  = defaultdict(int)
        wrong_params    = defaultdict(list)

        for e in entries:
            result = test_address(e, rules, verbose=args.verbose)
            if result["recall"] is not None:
                recalls.append(result["recall"])
            if result["precision"] is not None:
                precisions.append(result["precision"])

            for param, (status, exp_val, matched) in result["checks"].items():
                if status == "MISSING":
                    missing_params[param] += 1
                elif status == "WRONG":
                    wrong_params[param].append((e["address"][:30], exp_val,
                                                [r["value"] for r in matched]))

        avg_recall    = sum(recalls)    / len(recalls)    if recalls    else 0
        avg_precision = sum(precisions) / len(precisions) if precisions else 0

        recalls_pct    = f"{avg_recall*100:.0f}%"
        precision_pct  = f"{avg_precision*100:.0f}%"
        rc = GREEN if avg_recall >= 0.9 else (YELLOW if avg_recall >= 0.7 else RED)
        pc = GREEN if avg_precision >= 0.9 else (YELLOW if avg_precision >= 0.7 else RED)

        print(f"  Recall:    {c(rc, recalls_pct):20} "
              f"(params found / params expected, excl. qualitative)")
        print(f"  Precision: {c(pc, precision_pct):20} "
              f"(correct values / params found)")

        if missing_params:
            print(f"\n  Missing parameters (not extracted):")
            for param, count in sorted(missing_params.items(), key=lambda x: -x[1]):
                print(f"    {c(RED,'✗')} {param}: {count} address(es)")

        if wrong_params:
            print(f"\n  Wrong values (extracted but value mismatch):")
            for param, cases in sorted(wrong_params.items()):
                for addr, exp, got in cases[:2]:
                    print(f"    {c(YELLOW,'~')} {param}: expected={exp}, got={got}  [{addr}]")

        grand_recall.extend(recalls)
        grand_precision.extend(precisions)

    if len(by_routing) > 1:
        gr = sum(grand_recall)    / len(grand_recall)    if grand_recall    else 0
        gp = sum(grand_precision) / len(grand_precision) if grand_precision else 0
        print(f"\n{'='*65}")
        print(c(BOLD, f"Overall — {len(verified)} addresses"))
        rc = GREEN if gr >= 0.9 else (YELLOW if gr >= 0.7 else RED)
        pc = GREEN if gp >= 0.9 else (YELLOW if gp >= 0.7 else RED)
        print(f"  Recall:    {c(rc, f'{gr*100:.0f}%')}")
        print(f"  Precision: {c(pc, f'{gp*100:.0f}%')}")


if __name__ == "__main__":
    main()
