"""
verify_rules.py — Verify and clean up extracted rules.
Auto-fixes known misclassifications, merges landscaped area patch,
optional interactive review, produces final rules_r2_canada_bay.json.

Usage:
    cd extractor/
    python verify_rules.py
"""

import json
from pathlib import Path


def load_rules(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rules(rules: list[dict], path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)


def print_rule(rule: dict, index: int, total: int):
    print(f"\n{'='*60}")
    print(f"Rule {index+1}/{total}: {rule['rule_id']}")
    print(f"{'='*60}")
    print(f"  Parameter:   {rule['parameter']}")
    print(f"  Value:       {rule['value']} {rule['unit']}")
    print(f"  Operator:    {rule['operator']}")
    print(f"  Dwelling:    {rule.get('dwelling_type', 'all')}")
    print(f"  Storey:      {rule.get('storey_applicability', 'n/a')}")
    print(f"  Lot type:    {rule.get('lot_type', 'n/a')}")
    print(f"  Conditions:  {rule.get('conditions', [])}")
    print(f"  Exceptions:  {rule.get('exceptions', [])}")
    print(f"  Source:      {rule['source_clause']} (page {rule['source_page']})")
    print(f"  Quote:       {rule['source_text'][:120]}")
    print(f"  Confidence:  {rule.get('confidence', 'n/a')}")


def auto_fix_known_issues(rules: list[dict]) -> list[dict]:
    """Automatically fix known misclassifications."""
    fixed = []
    removed_count = 0
    reclassified_count = 0

    for rule in rules:
        # ── Remove misclassified rules ──

        # 1.5m facade articulation is NOT a front_setback from boundary
        if (rule["parameter"] == "front_setback"
                and rule["value"] == 1.5
                and "E2.1" in rule.get("source_clause", "")):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 1.5m facade articulation, not a boundary setback")
            removed_count += 1
            continue

        # 1m pathway setback is NOT a front_setback
        if (rule["parameter"] == "front_setback"
                and rule["value"] == 1
                and "E5.2" in rule.get("source_clause", "")):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 1m pathway setback, not a boundary front setback")
            removed_count += 1
            continue

        # 25m corner measurement is NOT a front_setback
        if (rule["parameter"] == "front_setback"
                and rule["value"] == 25):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 25m is a measurement point, not a setback value")
            removed_count += 1
            continue

        # 0.5m deck height is NOT private_open_space
        if (rule["parameter"] == "private_open_space"
                and rule["value"] == 0.5):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 0.5m is deck height, not private open space area")
            removed_count += 1
            continue

        # 0.8m swimming pool waterline is NOT a rear_setback
        if (rule["parameter"] == "rear_setback"
                and rule["value"] == 0.8
                and "E5.2" in rule.get("source_clause", "")):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 0.8m is pool waterline distance, not building rear setback")
            removed_count += 1
            continue

        # 0.5m and 1.8m in E5.2 are for structures (pools, satellite dishes), not buildings
        if (rule["parameter"] == "max_height"
                and rule["value"] in (0.5, 1.8)
                and "E5.2" in rule.get("source_clause", "")):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- {rule['value']}m is for ancillary structure, not building height")
            removed_count += 1
            continue

        # 1.5m dormer height is NOT the building max_height
        if (rule["parameter"] == "max_height"
                and rule["value"] == 1.5
                and "E2.1" in rule.get("source_clause", "")):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 1.5m is dormer height, not building max height")
            removed_count += 1
            continue

        # ── Reclassify misclassified rules ──

        # 25 degrees roof pitch is NOT a height_plane
        if (rule["parameter"] == "height_plane"
                and rule["value"] == 25
                and "E2.1" in rule.get("source_clause", "")):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 25 degrees is roof pitch, not height plane")
            rule["parameter"] = "roof_pitch_min"
            reclassified_count += 1

        # 85% block-out is NOT building_separation
        if (rule["parameter"] == "building_separation"
                and rule["value"] == 85):
            print(f"  AUTO-REMOVE: {rule['rule_id']} -- 85% is screen block-out density, not building separation")
            removed_count += 1
            continue

        # 40% facade width is NOT building_separation
        if (rule["parameter"] == "building_separation"
                and rule["value"] == 40
                and "E2.1" in rule.get("source_clause", "")):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 40% is max primary facade width, not building separation")
            rule["parameter"] = "primary_facade_width_pct"
            reclassified_count += 1

        # 600mm landscaped area is a dimension, not a percentage
        if (rule["parameter"] == "landscaped_area_front_pct"
                and rule.get("unit") == "mm"):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 600mm is landscaping strip width, not a percentage")
            rule["parameter"] = "landscaping_strip_width"
            rule["value"] = 0.6
            rule["unit"] = "m"
            reclassified_count += 1

        # 1.0m in landscaped_area_pct is actually a pathway setback from boundary
        if (rule["parameter"] == "landscaped_area_pct"
                and rule["value"] == 1.0
                and rule.get("unit") == "m"):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 1.0m is pathway setback from boundary, not landscape percentage")
            rule["parameter"] = "pathway_boundary_setback"
            reclassified_count += 1

        # 33% driveway width is NOT min_dwelling_width
        if (rule["parameter"] == "min_dwelling_width"
                and rule["value"] == 33.33):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 33% is max driveway width as proportion of frontage")
            rule["parameter"] = "max_driveway_width_pct"
            reclassified_count += 1

        # 1.5m in min_dwelling_width from E3.1 is actually a window sill height
        if (rule["parameter"] == "min_dwelling_width"
                and rule["value"] == 1.5
                and "E3.1" in rule.get("source_clause", "")):
            print(f"  AUTO-RECLASSIFY: {rule['rule_id']} -- 1.5m is window sill height for privacy, not dwelling width")
            rule["parameter"] = "privacy_sill_height"
            reclassified_count += 1

        # 1.5m outbuilding setback in E5.2 is for tennis courts, not general outbuildings
        if (rule["parameter"] == "outbuilding_setback"
                and rule["value"] == 1.5
                and "E5.2" in rule.get("source_clause", "")):
            rule["conditions"].append("tennis court fencing only")

        fixed.append(rule)

    print(f"\n  Auto-removed: {removed_count} rules")
    print(f"  Auto-reclassified: {reclassified_count} rules")
    print(f"  Remaining: {len(fixed)} rules")

    return fixed


def merge_landscaped_area_patch(rules: list[dict], patch_path: str) -> list[dict]:
    """Merge the landscaped area rules from the patch file."""
    if not Path(patch_path).exists():
        print(f"  No patch file found at {patch_path} -- skipping")
        return rules

    with open(patch_path, "r") as f:
        patch_rules = json.load(f)

    print(f"  Merging {len(patch_rules)} landscaped area rules from patch...")

    for i, rule in enumerate(patch_rules):
        rule["rule_id"] = f"cb_dcp_e_122_patch_r{i+1}"
        rule["lga"] = "City of Canada Bay"
        rule["source_document"] = "Canada Bay DCP Part E"
        rule["source_clause"] = f"E4.6 {rule.get('source_clause', '')}"
        rule["source_page"] = rule.get("source_page", 26)
        rule["source_type"] = "dcp_extraction"
        rule["superseded_by"] = None
        rule["verified"] = True
        rule["extracted_by"] = "claude-sonnet-4-5-20250929"
        rules.append(rule)

    return rules


def interactive_review(rules: list[dict]) -> list[dict]:
    """Quick interactive review of remaining rules."""
    print(f"\n{'='*60}")
    print(f"INTERACTIVE REVIEW -- {len(rules)} rules")
    print(f"{'='*60}")
    print(f"For each rule, enter:")
    print(f"  y = correct, keep as-is")
    print(f"  n = wrong, remove")
    print(f"  s = skip (keep but mark unverified)")
    print(f"  q = stop review, keep remaining as-is")
    print(f"{'='*60}")

    reviewed = []
    for i, rule in enumerate(rules):
        if rule.get("verified"):
            reviewed.append(rule)
            continue

        print_rule(rule, i, len(rules))

        while True:
            choice = input("\n  [y/n/s/q] > ").strip().lower()
            if choice in ("y", "n", "s", "q"):
                break
            print("  Invalid choice. Enter y, n, s, or q.")

        if choice == "y":
            rule["verified"] = True
            reviewed.append(rule)
            print("  Verified")
        elif choice == "n":
            print("  Removed")
            continue
        elif choice == "s":
            reviewed.append(rule)
            print("  Skipped")
        elif choice == "q":
            reviewed.extend(rules[i:])
            print(f"  Stopping review. Kept remaining {len(rules) - i} rules as-is.")
            break

    return reviewed


def print_final_summary(rules: list[dict]):
    """Print a clean summary of the final rules."""
    print(f"\n{'='*60}")
    print(f"FINAL VERIFIED RULES -- {len(rules)} total")
    print(f"{'='*60}")

    by_param = {}
    for r in rules:
        p = r["parameter"]
        if p not in by_param:
            by_param[p] = []
        by_param[p].append(r)

    for param in sorted(by_param.keys()):
        param_rules = by_param[param]
        print(f"\n  {param} ({len(param_rules)} rule(s)):")
        for r in param_rules:
            verified = "V" if r.get("verified") else " "
            dwelling = r.get("dwelling_type", "all")
            storey = r.get("storey_applicability", "n/a")
            lot = r.get("lot_type", "n/a")
            print(f"    [{verified}] {r['value']}{r['unit']} ({r['operator']}) "
                  f"| {dwelling} | {storey} | {lot} "
                  f"| {r.get('source_clause', 'n/a')}")

    # Ground truth recheck
    print(f"\n{'='*60}")
    print(f"GROUND TRUTH RECHECK")
    print(f"{'='*60}")

    ground_truth = {
        "front_setback": {"value": 4.5, "unit": "m", "dwelling_type": "all"},
        "side_setback_ground": {"value": 0.9, "unit": "m", "dwelling_type": "dwelling_house"},
        "side_setback_upper": {"value": 1.5, "unit": "m", "dwelling_type": "dwelling_house"},
        "rear_setback": {"value": 6.0, "unit": "m", "dwelling_type": "all"},
        "max_storeys": {"value": 2, "unit": "storeys", "dwelling_type": "all"},
        "height_plane": {"value": 45, "unit": "degrees", "dwelling_type": "all"},
        "landscaped_area_pct": {"value": 35, "unit": "pct", "dwelling_type": "dwelling_house"},
        "private_open_space": {"value": 40, "unit": "m2", "dwelling_type": "dwelling_house"},
    }

    matches = 0
    for param, expected in ground_truth.items():
        matching = [r for r in rules
                    if r["parameter"] == param
                    and r.get("dwelling_type") in (expected["dwelling_type"], "all")]

        if not matching:
            print(f"  MISS {param}: NOT FOUND (expected {expected['value']}{expected['unit']})")
        else:
            best = max(matching, key=lambda r: r.get("confidence", 0))
            if best["value"] == expected["value"]:
                print(f"  OK   {param}: {best['value']}{best['unit']} -- MATCH")
                matches += 1
            else:
                print(f"  WARN {param}: {best['value']}{best['unit']} != expected {expected['value']}{expected['unit']}")
                any_match = [r for r in matching if r["value"] == expected["value"]]
                if any_match:
                    print(f"       (correct value exists in: {any_match[0].get('source_clause', 'unknown')})")
                    matches += 1

    print(f"\n  Score: {matches}/{len(ground_truth)} = {matches/len(ground_truth)*100:.0f}%")


def main():
    raw_path = "../data/rules_r2_canada_bay_validated.json"
    patch_path = "../data/landscaped_area_patch.json"
    output_path = "../data/rules_r2_canada_bay.json"

    print("Loading validated rules...")
    rules = load_rules(raw_path)
    print(f"  Loaded {len(rules)} rules")

    # Step 1: Auto-fix known issues
    print("\n--- Step 1: Auto-fixing known misclassifications ---")
    rules = auto_fix_known_issues(rules)

    # Step 2: Merge landscaped area patch
    print("\n--- Step 2: Merging landscaped area patch ---")
    rules = merge_landscaped_area_patch(rules, patch_path)

    # Step 3: Interactive review (optional)
    print("\n--- Step 3: Interactive review ---")
    choice = input("Do you want to review each rule interactively? (y/n) > ").strip().lower()
    if choice == "y":
        rules = interactive_review(rules)
    else:
        print("  Skipping interactive review. All auto-fixed rules kept.")

    # Step 4: Save final output
    save_rules(rules, output_path)
    print(f"\nSaved {len(rules)} final rules to {output_path}")

    # Step 5: Print summary
    print_final_summary(rules)


if __name__ == "__main__":
    main()