"""
verify_ground_truth.py — Interactive CLI to manually verify ground truth addresses.

For each pending address, shows every rule and lets you:
  - Confirm the value is correct (Enter)
  - Override with a different value (type new value)
  - Mark as N/A — rule doesn't apply to this address (n)
  - Add a note (!)
  - Skip the address entirely (s)
  - Quit and save progress (q)

Usage:
    python verify_ground_truth.py
    python verify_ground_truth.py --all        # include already-verified addresses
    python verify_ground_truth.py --lga Ashfield
"""

import json
import sys
import argparse
from pathlib import Path
from copy import deepcopy

GT_PATH = Path("data/ground_truth_addresses.json")

YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[91m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def c(color, text):
    return f"{color}{text}{RESET}"


def print_header(address, lga, zone, verified_via, idx, total):
    print("\n" + "=" * 70)
    print(c(BOLD, f"[{idx}/{total}] {address}"))
    print(f"  LGA: {lga}  |  Zone: {zone}  |  Verified: {c(YELLOW, verified_via)}")
    print("=" * 70)


def prompt_rule(param, current_value):
    """
    Show a rule and ask user to confirm or override.
    Returns (new_value, note, action)
      action: 'keep' | 'override' | 'na' | 'skip_address' | 'quit'
    """
    print(f"\n  {c(CYAN, param)}")
    print(f"  Current value: {c(BOLD, current_value)}")
    print(f"  {c(DIM, '[Enter]=confirm  [new value]=override  [n]=N/A  [!note]=add note  [s]=skip address  [q]=quit')}")

    raw = input("  > ").strip()

    if raw == "":
        return current_value, None, "keep"
    if raw.lower() == "q":
        return current_value, None, "quit"
    if raw.lower() == "s":
        return current_value, None, "skip_address"
    if raw.lower() == "n":
        return "N/A", None, "na"
    if raw.startswith("!"):
        note = raw[1:].strip()
        return current_value, note, "keep"
    # treat as override
    return raw, None, "override"


def verify_address(entry, idx, total):
    """
    Walk through all rules for one address interactively.
    Returns modified entry, or None if user quit.
    """
    entry = deepcopy(entry)
    address = entry["address"]
    lga     = entry.get("lga", "?")
    zone    = entry.get("expected_zone", "?")
    verified = entry.get("verified_via", "pending")
    dcp     = entry.get("dcp_routing", "")
    note    = entry.get("note", "")

    print_header(address, lga, zone, verified, idx, total)
    if dcp:
        print(f"  DCP routing: {dcp}")
    if note:
        print(f"  Note: {c(DIM, note)}")

    checks = entry.get("check", {})
    updated_checks = {}
    address_notes = []

    for param, value in checks.items():
        new_val, extra_note, action = prompt_rule(param, value)

        if action == "quit":
            return entry, "quit"
        if action == "skip_address":
            print(c(YELLOW, "  Skipping address."))
            return entry, "skip"

        if action == "override":
            print(c(GREEN, f"  Updated: {param} = {new_val}"))
        elif action == "na":
            print(c(DIM, f"  Marked N/A: {param}"))
        elif extra_note:
            print(c(CYAN, f"  Note added: {extra_note}"))

        updated_checks[param] = new_val
        if extra_note:
            address_notes.append(f"{param}: {extra_note}")

    # Ask for zone confirmation
    print(f"\n  {c(CYAN, 'expected_zone')}")
    print(f"  Current value: {c(BOLD, zone)}")
    print(f"  {c(DIM, '[Enter]=confirm  [R3/other]=override  [s]=skip  [q]=quit')}")
    raw = input("  > ").strip()
    if raw.lower() == "q":
        return entry, "quit"
    if raw.lower() == "s":
        return entry, "skip"
    if raw:
        entry["expected_zone"] = raw
        print(c(GREEN, f"  Zone updated: {raw}"))

    # Ask verified_via
    print(f"\n  {c(CYAN, 'verified_via')}")
    print(f"  Current: {c(YELLOW, verified)}")
    print(f"  {c(DIM, '[Enter]=keep  [api]=NSW Planning API  [pdf]=PDF only  [other text]=custom')}")
    raw = input("  > ").strip()
    if raw.lower() == "q":
        return entry, "quit"
    if raw.lower() == "api":
        entry["verified_via"] = "NSW Planning API"
        print(c(GREEN, "  Marked as NSW Planning API verified."))
    elif raw:
        entry["verified_via"] = raw
        print(c(GREEN, f"  verified_via = {raw}"))

    entry["check"] = updated_checks
    if address_notes:
        existing = entry.get("note", "")
        entry["note"] = (existing + " | " if existing else "") + " | ".join(address_notes)

    print(c(GREEN, f"\n  ✓ Address complete."))
    return entry, "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",  action="store_true", help="Include already-verified addresses")
    parser.add_argument("--lga",  default="",          help="Filter to one LGA (e.g. Ashfield)")
    args = parser.parse_args()

    data = json.loads(GT_PATH.read_text(encoding="utf-8"))

    # Filter to addresses to work on
    targets = []
    for i, entry in enumerate(data):
        if args.lga and entry.get("lga", "").lower() != args.lga.lower():
            continue
        if not args.all and entry.get("verified_via", "pending") != "pending":
            continue
        targets.append((i, entry))

    if not targets:
        print("No pending addresses to verify." +
              (" Use --all to re-verify confirmed ones." if not args.all else ""))
        sys.exit(0)

    print(c(BOLD, f"\nGround Truth Verifier — {len(targets)} addresses to check"))
    print("Keys: Enter=confirm | new value=override | n=N/A | !note=add note | s=skip | q=quit\n")

    for count, (orig_idx, entry) in enumerate(targets, 1):
        updated_entry, action = verify_address(entry, count, len(targets))
        data[orig_idx] = updated_entry

        # Save after every address so progress isn't lost
        GT_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        if action == "quit":
            print(c(YELLOW, "\nProgress saved. Run again to continue."))
            sys.exit(0)

    print(c(GREEN, f"\n{'='*70}"))
    print(c(GREEN, c(BOLD, "All addresses verified. File saved.")))
    verified_count = sum(1 for e in data if e.get("verified_via") != "pending")
    print(f"Total verified: {verified_count}/{len(data)}")


if __name__ == "__main__":
    main()
