"""
test_accuracy.py — Test extracted rules against verified ground truth.

For each verified address in data/ground_truth_addresses.json, loads the
rules file for that LGA and checks:
  - Recall: what fraction of GT parameters are covered by extracted rules
  - Precision: of parameters covered, how many values match

It also runs a separate envelope-geometry check (see test_envelope_geometry
below): the rule-accuracy check above only verifies that DCP text was
parsed into the right VALUE (e.g. front_setback=4.5m) — it never builds a
polygon, so it can't catch a bug in how that value gets applied to actual
lot geometry (this project shipped exactly that kind of bug once: setbacks
were silently under-applied by ~17% on east/west-facing lots because 1
degree of longitude and 1 degree of latitude were treated as the same
physical distance).

Usage:
    python test_accuracy.py                          # test all LGAs
    python test_accuracy.py --lga "Canada Bay"       # one LGA
    python test_accuracy.py --verbose                # show per-param detail
    python test_accuracy.py --skip-geometry          # skip the envelope check
"""

import json
import re
import sys
import argparse
from pathlib import Path
from collections import defaultdict

from shapely.geometry import shape, Point
from shapely.ops import nearest_points

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
from compute_envelope import compute_envelope, get_front_edge  # noqa: E402
from check_lmr import haversine  # noqa: E402  (independent great-circle distance — not compute_envelope's own projection math)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
GT_PATH  = BASE_DIR / "data" / "ground_truth_addresses.json"

# Maps dcp_routing → rules file (current hand-tuned files)
RULES_FILES = {
    "canada_bay":                    BASE_DIR / "data" / "rules_r2_canada_bay.json",
    "canada_bay_r3":                 BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",
    "canada_bay_r4":                 BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",  # R4 shares R3's Part F ruleset
}

# Pipeline output files (used when --pipeline flag is set)
PIPELINE_FILES = {
    "canada_bay":                    BASE_DIR / "data" / "rules_r2_canada_bay_pipeline.json",
    "canada_bay_r3":                 BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",
    "canada_bay_r4":                 BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",
}

# LGA label → dcp_routing key. Canada Bay is zone-dependent (R2 uses Part E,
# R3/R4 both use Part F) — resolved per-entry against the GT record's "zone"
# field, not just the LGA name; see the routing normalization below.
LGA_ROUTING_MAP = {
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


# ── Envelope geometry test ──────────────────────────────────────────────────
#
# Builds a real envelope for every cached lot (data/cached_lots.json) and
# checks three things the rule-accuracy test above never touches:
#   1. compute_envelope() runs without raising, for every zone Canada Bay
#      serves live (R1-R4).
#   2. The resulting envelope is non-empty and strictly smaller than the lot
#      — a basic sanity bound.
#   3. The front and rear setback distances actually applied are within
#      tolerance of the DCP-cited value, measured independently via the
#      haversine great-circle formula rather than compute_envelope's own
#      equirectangular projection — so a regression of the same
#      longitude-compression bug (setbacks silently off by up to ~17% on
#      east/west-facing lots) would show up here as a measured-vs-expected
#      mismatch, not just a "didn't crash" false pass.

CACHE_PATH = BASE_DIR / "data" / "cached_lots.json"

ENVELOPE_RULES_FILES = {
    "R2": BASE_DIR / "data" / "rules_r2_canada_bay.json",
    "R3": BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",
    "R4": BASE_DIR / "data" / "rules_r3_canada_bay_pipeline.json",  # R4 shares R3's Part F ruleset, same as main.py
}

SETBACK_TOLERANCE_M   = 0.3   # absolute floor
SETBACK_TOLERANCE_PCT = 0.10  # relative — whichever tolerance is larger wins


def _rules_for_zone(zone: str) -> tuple[list[dict], str]:
    # R1 permits a dwelling house under the same Part E rules R2 uses — same
    # routing main.py applies before calling compute_envelope. Returns the
    # zone string rules are actually tagged with (R1 -> "R2"), needed by
    # _build_env_rules below.
    lookup_zone = "R2" if zone == "R1" else zone
    path = ENVELOPE_RULES_FILES.get(lookup_zone)
    if not path or not path.exists():
        return [], lookup_zone
    all_rules = json.loads(path.read_text(encoding="utf-8"))
    confident = [r for r in all_rules if r.get("confidence", 0) >= 0.8]  # matches main.py's live filter
    return confident, lookup_zone


def _build_env_rules(rules: list[dict], matching_zone: str) -> list[dict]:
    """Reproduces main.py's _build_scenario rule-selection order (zone
    match, then dwelling_house/all preferred over other dwelling types per
    parameter). compute_envelope() does NO zone/dwelling-type filtering
    internally — it trusts the caller to have already narrowed `rules` down
    to the right rows first, exactly like this. Skipping this step (as an
    earlier version of this test did) meant compute_envelope() was handed a
    rule list mixing R3/R4/other-dwelling-type entries, so its "first
    matching rule" pick could silently be for the wrong zone/dwelling type
    — producing a false mismatch that had nothing to do with the geometry
    being tested."""
    zone_rules = [
        r for r in rules
        if r.get("zone") in (matching_zone, "all_residential", "all")
        and not r.get("superseded_by")
    ]
    env_rules = [r for r in zone_rules if r.get("dwelling_type") in ("dwelling_house", "all")]
    covered = {r["parameter"] for r in env_rules}
    env_rules += [
        r for r in zone_rules
        if r.get("dwelling_type") not in ("dwelling_house", "all")
        and r["parameter"] not in covered
    ]
    return env_rules


def _first_setback(rules: list[dict], param: str):
    """Mirrors compute_envelope's own first-match rule selection, so the
    'expected' value here is whatever the DCP actually states today — not a
    hardcoded number that would silently go stale if the rules change."""
    for r in rules:
        if r.get("parameter") == param and r.get("operator") == "min":
            return r["value"]
    return None


def _measured_setback_m(envelope_geom, edge_p1, edge_p2) -> float:
    mid = Point((edge_p1[0] + edge_p2[0]) / 2, (edge_p1[1] + edge_p2[1]) / 2)
    nearest_on_env, _ = nearest_points(envelope_geom.boundary, mid)
    return haversine(mid.y, mid.x, nearest_on_env.y, nearest_on_env.x)


def test_envelope_geometry(verbose: bool = False) -> dict:
    if not CACHE_PATH.exists():
        print(c(RED, "\nNo cached_lots.json found — skipping envelope geometry test."))
        return {}

    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    total, crashed, bad_bounds = 0, 0, 0
    setback_checked, setback_failed = 0, 0
    crash_list, bad_bounds_list, setback_fail_list = [], [], []

    for address, lot in cache.items():
        zone, polygon = lot.get("zone"), lot.get("polygon")
        if zone not in ("R1", "R2", "R3", "R4") or not polygon:
            continue
        # A handful of cached entries pre-date the Canada-Bay-only cleanup
        # (Inner West addresses from when Marrickville/Ashfield were also
        # supported). Their geometry is fine to clip, but comparing the
        # result against CANADA BAY's setback values would be comparing two
        # different councils' standards — skip them rather than report a
        # false mismatch that has nothing to do with the geometry code.
        if lot.get("lga_name") and lot["lga_name"].upper() != "CITY OF CANADA BAY" and lot["lga_name"].upper() != "CANADA BAY":
            continue
        total += 1

        raw_rules, matching_zone = _rules_for_zone(zone)
        rules = _build_env_rules(raw_rules, matching_zone)
        try:
            env_geojson, _warnings = compute_envelope(polygon, rules, lot["lat"], lot["lon"])
        except Exception as e:
            crashed += 1
            crash_list.append((address, str(e)))
            continue

        lot_geom = shape(polygon)
        env_geom = shape(env_geojson)

        if env_geom.is_empty or env_geom.area <= 0 or env_geom.area >= lot_geom.area:
            bad_bounds += 1
            bad_bounds_list.append(address)
            continue

        coords = polygon["coordinates"][0]
        ring = coords[:-1]
        n = len(ring)
        front_idx, _ = get_front_edge(coords, lot["lon"], lot["lat"])
        fp1, fp2 = ring[front_idx], ring[(front_idx + 1) % n]
        fmid = ((fp1[0] + fp2[0]) / 2, (fp1[1] + fp2[1]) / 2)

        # Rear edge = midpoint furthest from the front edge midpoint — same
        # heuristic compute_envelope uses internally, duplicated here since
        # it's simple/stable and not part of the projection math being
        # tested. Distance MUST be measured in real metres (haversine) here,
        # not raw lon/lat degrees: degree-space distance is anisotropic
        # (longitude is compressed relative to latitude), so on a narrow,
        # deep lot a long SIDE edge's midpoint can appear farther from the
        # front in raw degrees than the true rear edge is — silently
        # comparing this test against the wrong edge and reporting a false
        # setback mismatch.
        rear_idx, rear_dist = front_idx, -1.0
        for i in range(n):
            if i == front_idx:
                continue
            p1, p2 = ring[i], ring[(i + 1) % n]
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            d = haversine(fmid[1], fmid[0], my, mx)
            if d > rear_dist:
                rear_dist, rear_idx = d, i
        rp1, rp2 = ring[rear_idx], ring[(rear_idx + 1) % n]

        for label, p1, p2, expected in (
            ("front", fp1, fp2, _first_setback(rules, "front_setback")),
            ("rear",  rp1, rp2, _first_setback(rules, "rear_setback")),
        ):
            if expected is None:
                continue
            setback_checked += 1
            measured = _measured_setback_m(env_geom, p1, p2)
            tol = max(SETBACK_TOLERANCE_M, expected * SETBACK_TOLERANCE_PCT)
            ok = abs(measured - expected) <= tol
            if not ok:
                setback_failed += 1
                setback_fail_list.append((address, label, expected, round(measured, 2)))
            if verbose:
                mark = c(GREEN, "✓") if ok else c(RED, "✗")
                print(f"    {mark} {address[:40]:40} {label:5} "
                      f"expected={expected}m measured={measured:.2f}m")

    print(f"\n{c(BOLD, 'Envelope Geometry')}  ({total} cached lots checked)")
    print("-" * 55)

    ok_computed = total - crashed
    crash_c = GREEN if crashed == 0 else RED
    print(f"  Computed successfully:   {c(crash_c, f'{ok_computed}/{total}')}")
    for addr, err in crash_list[:5]:
        print(f"    {c(RED,'✗')} {addr}: {err}")

    ok_bounds = ok_computed - bad_bounds
    bounds_c = GREEN if bad_bounds == 0 else RED
    print(f"  Within lot boundary:     {c(bounds_c, f'{ok_bounds}/{ok_computed}')}")
    for addr in bad_bounds_list[:5]:
        print(f"    {c(RED,'✗')} {addr}: envelope empty, or >= lot area")

    sb_c = GREEN if setback_failed == 0 else RED
    print(f"  Setback distance correct (±{SETBACK_TOLERANCE_M}m or {int(SETBACK_TOLERANCE_PCT*100)}%, "
          f"front+rear): {c(sb_c, f'{setback_checked - setback_failed}/{setback_checked}')}")
    for addr, label, exp, got in setback_fail_list[:8]:
        print(f"    {c(RED,'✗')} {addr[:40]:40} {label:5} expected={exp}m got={got}m")

    return {
        "total": total, "crashed": crashed, "bad_bounds": bad_bounds,
        "setback_checked": setback_checked, "setback_failed": setback_failed,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lga",      default="", help="Filter to one LGA label")
    parser.add_argument("--rules",    default="", help="Override rules file path")
    parser.add_argument("--pipeline", action="store_true",
                        help="Use pipeline output files instead of hand-tuned files")
    parser.add_argument("--verbose",  action="store_true", help="Show per-param detail")
    parser.add_argument("--skip-geometry", action="store_true",
                        help="Skip the envelope-geometry check (rule accuracy only)")
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
        # Normalize Canada Bay routing — zone-dependent: R2 uses Part E
        # (canada_bay), R3/R4 both use Part F (canada_bay_r3/r4).
        if "canada" in routing.lower() or e.get("lga","").lower() == "city of canada bay":
            zone = e.get("expected_zone", "R2")
            routing = {"R2": "canada_bay", "R3": "canada_bay_r3", "R4": "canada_bay_r4"}.get(zone, "canada_bay")
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

    if not args.skip_geometry:
        print(f"\n{'='*65}")
        test_envelope_geometry(verbose=args.verbose)


if __name__ == "__main__":
    main()
