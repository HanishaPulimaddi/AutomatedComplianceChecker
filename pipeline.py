"""
pipeline.py — Generic DCP/LEP rule extraction pipeline.

Reads pipeline_manifest.json. For each LGA entry:
  1. Auto-detects clause style from the PDF (C-style, DS-style, E-section, etc.)
  2. Chunks PDF with full section hierarchy embedded in every chunk
  3. Filters chunks to R2-scoped sections (if r2_scope is set in manifest)
  4. Calls Claude to extract rules with full context (conditions, exceptions, routing)
  5. Validates schema, deduplicates, merges LEP rules
  6. Writes output rules JSON

Usage:
    python pipeline.py                          # run all LGAs in manifest
    python pipeline.py --lga Ashfield           # run one LGA
    python pipeline.py --lga Ashfield --dry-run # show chunks, no API calls
    python pipeline.py --lga Ashfield --reuse-chunks  # skip re-chunking
"""

import json
import os
import re
import sys
import time
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = BASE_DIR / "data"
CHUNKS_DIR = DATA_DIR / "pipeline_chunks"
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL  = "claude-sonnet-4-6"

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Clause style auto-detection ─────────────────────────────────────────────

CLAUSE_PATTERNS = {
    "C_style":  re.compile(r"^C\d+[a-z]?\.?\s"),           # C10. or C10 text
    "DS_style": re.compile(r"^DS\d+\.\d+\.?\s"),            # DS3.4 text
    "E_style":  re.compile(r"^E\d+(?:\.\d+)*\s"),           # E2.1 section
    "cl_style": re.compile(r"^cl(?:ause)?\s+\d+", re.I),   # cl 4.3 / clause 4.3
    "num_style": re.compile(r"^\d+\.\d+(?:\.\d+)*\s"),     # 4.1.5 text
    "PC_style": re.compile(r"^PC\d+\.\d+\.?\s"),            # PC2.1 text
    "AS_style": re.compile(r"^AS\d+\.\d+\.?\s"),            # AS2.1 text
}

def detect_clause_style(doc, sample_pages: int = 20) -> list[str]:
    """Scan first N pages and return ranked list of clause styles found."""
    counts = Counter()
    for page in doc[:sample_pages]:
        for line in page.get_text("text").split("\n"):
            line = line.strip()
            for name, pat in CLAUSE_PATTERNS.items():
                if pat.match(line):
                    counts[name] += 1
    return [style for style, _ in counts.most_common()]


# ── Page-header Part detection ───────────────────────────────────────────────

# Matches "Part 1", "Part 1−", "Part 1 Dwelling Houses" in page headers
_PART_HEADER = re.compile(
    r"Part\s+(\d+)[\s\-–—]*([A-Za-z][^\n]*)?", re.IGNORECASE
)
# Matches "E2.1", "E3", etc. in Canada Bay Part E
_E_SECTION_HDR = re.compile(r"^E(\d+(?:\.\d+)*)\s+(.+)", re.IGNORECASE)
# Matches "C1.1", "C3" in Canada Bay Part C
_C_SECTION_HDR = re.compile(r"^(C\d+(?:\.\d+)*)\s{2,}(.+)")


def get_page_part(page_text: str) -> str | None:
    """Extract the Part name from page header text (e.g. 'Part 1 Dwelling Houses')."""
    for line in page_text.split("\n")[:8]:
        m = _PART_HEADER.search(line.strip())
        if m:
            num   = m.group(1)
            title = (m.group(2) or "").strip().rstrip("−–—").strip()
            return f"Part {num}" + (f" {title}" if title else "")
    return None


def get_body_font_size(doc) -> float:
    sizes = []
    for page in doc[:10]:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes.append(span["size"])
    counts = Counter(round(s) for s in sizes)
    return counts.most_common(1)[0][0] if counts else 10


EXTRACT_MEAS = re.compile(
    r"(\d+\.?\d*)\s*(mm|m\b|metres?|%|degrees?|m2|sqm|storeys?|ratio|spaces?)",
    re.IGNORECASE,
)

def extract_measurements(text: str) -> list[str]:
    return [m[0] + m[1] for m in EXTRACT_MEAS.findall(text)]


SKIP_LINES = {
    "Controls", "Objectives", "Objective", "Control", "Background",
    "Performance Criteria", "Design Solution", "Performance Criteria and Design Solutions",
    "Marrickville Development Control Plan 2011",
    "Comprehensive Inner West DCP 2016",
    "Canada Bay DCP - Part C", "Canada Bay DCP - Part E",
    "CITY OF CANADA BAY",
    "Chapter F \u2013 Development Category Guidelines",
    "Low Density Residential Development",
}


# ── Core PDF chunker ─────────────────────────────────────────────────────────

def chunk_pdf(pdf_cfg: dict, lga_label: str) -> list[dict]:
    """
    Parse one PDF into chunks. Each chunk carries:
      - section_path:  full routing path, e.g.
                       "Canada Bay DCP Part E > E4 Setbacks > C10"
      - part_context:  top-level Part name (from page header), e.g. "Part 1 Dwelling Houses"
      - section:       sub-section number/heading
      - clause_type:   control | objective | heading | body
      - measurements:  list of numeric values found
    """
    path            = pdf_cfg["path"]
    source_doc      = pdf_cfg["source_document"]
    skip_pages      = pdf_cfg.get("skip_pages", 0)

    doc          = fitz.open(path)
    body_size    = get_body_font_size(doc)
    heading_thr  = body_size + 1.5

    # Detect clause style
    clause_styles = detect_clause_style(doc)
    primary_style = clause_styles[0] if clause_styles else "C_style"

    # Build clause regex for this document's style(s)
    active_patterns = []
    for style in clause_styles[:3]:   # use top-3 styles found
        active_patterns.append((style, CLAUSE_PATTERNS[style]))

    # Additional patterns always checked
    ctrl_solo    = re.compile(r"^(C\d+[a-z]?)\.?\s*$")
    ctrl_inline  = re.compile(r"^(C\d+[a-z]?)\.?\s+(.+)", re.DOTALL)
    obj_solo     = re.compile(r"^(O\d+)\.?\s*$")
    obj_inline   = re.compile(r"^(O\d+)\.?\s+(.+)", re.DOTALL)
    ds_inline    = re.compile(r"^(DS\d+\.\d+)\.?\s*(.*)", re.DOTALL)
    subitem_pat  = re.compile(r"^[a-z]\)\s+|^\d+\.\s+|^[ivxIVX]+\.\s+")
    note_pat     = re.compile(r"^(Note|NB)[\s:]", re.IGNORECASE)
    # Section heading patterns
    num_section  = re.compile(r"^([A-Z]?\d+(?:\.\d+)+)\s+(.+)")  # E2.1 Title / 4.1.5 Title
    num_solo     = re.compile(r"^([A-Z]?\d+(?:\.\d+)+)$")

    chunks         = []
    chunk_counter  = 0
    current_part   = ""        # from page header: "Part 1 Dwelling Houses"
    current_sec    = ""        # sub-section: "E4" / "4.1.6"
    current_title  = ""        # sub-section title
    current_lines: list[str] = []
    current_page   = 1
    current_type   = "body"
    pending_label  = None
    pending_ltype  = None
    pending_secnum = None

    def routing_path(clause: str = "") -> str:
        parts = [source_doc]
        if current_part:
            parts.append(current_part)
        if current_sec:
            sec_label = current_sec + (f" {current_title}" if current_title else "")
            parts.append(sec_label)
        if clause:
            parts.append(clause)
        return " > ".join(parts)

    def flush():
        nonlocal chunk_counter
        text = re.sub(r"\s+", " ", " ".join(current_lines)).strip()
        if len(text) < 30:
            return
        chunks.append({
            "chunk_id":       f"{lga_label.lower().replace(' ','_')}_{chunk_counter:04d}",
            "source_document": source_doc,
            "part_context":   current_part,
            "section":        current_sec,
            "section_title":  current_title,
            "section_path":   routing_path(),
            "clause_type":    current_type,
            "page":           current_page,
            "text":           text,
            "measurements":   extract_measurements(text),
        })
        chunk_counter += 1

    for page_num, page in enumerate(doc, start=1):
        if page_num <= skip_pages:
            continue

        page_text = page.get_text("text")
        # Update part context from page header
        part = get_page_part(page_text)
        if part:
            current_part = part

        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue
            for line_obj in block["lines"]:
                line_text = "".join(s["text"] for s in line_obj["spans"]).strip()
                if not line_text:
                    continue

                max_font = max((s["size"] for s in line_obj["spans"]), default=0)
                is_bold  = any("Bold" in s.get("font","") or "bold" in s.get("font","")
                                for s in line_obj["spans"])
                is_large = max_font > heading_thr

                # Skip noise
                if line_text in SKIP_LINES:
                    continue
                if re.match(r"^(Version:|Document Set ID:|Page [A-Za-z0-9\-]+\s*$)", line_text):
                    continue
                if re.match(r"^(Figure|Table)\s+[A-Z0-9\-]", line_text):
                    continue
                if re.match(r"^\d+\s*$", line_text) or re.match(r"^[A-Z]-\d+\s*$", line_text):
                    continue
                if re.match(r"^page\s+[A-Z]?-?\d+\s*$", line_text, re.IGNORECASE):
                    continue

                # ── Section heading: "E2.1 Title" or "4.1.5 Title" on one line ──
                if (is_large or is_bold) and (m := num_section.match(line_text)):
                    flush()
                    current_lines  = []
                    pending_label  = None
                    pending_secnum = None
                    current_sec    = m.group(1)
                    current_title  = m.group(2).strip()
                    current_page   = page_num
                    current_type   = "heading"
                    continue

                # ── Section heading: bare number, title on next bold line ──
                if (is_large or is_bold) and (m := num_solo.match(line_text)):
                    flush()
                    current_lines  = []
                    pending_label  = None
                    pending_secnum = m.group(1)
                    current_page   = page_num
                    current_type   = "heading"
                    continue

                # ── Resolve pending section number ──
                if pending_secnum:
                    if is_bold or is_large:
                        current_sec    = pending_secnum
                        current_title  = line_text
                        pending_secnum = None
                        continue
                    else:
                        current_sec    = pending_secnum
                        current_title  = ""
                        pending_secnum = None

                # ── Font-size-only heading (Ashfield / large bold prose) ──
                if is_bold and max_font >= (body_size + 3):
                    flush()
                    current_lines  = []
                    pending_label  = None
                    current_title  = line_text
                    current_page   = page_num
                    current_type   = "heading"
                    continue

                # ── DS-style clause (Ashfield DS3.4) ──
                if m := ds_inline.match(line_text):
                    flush()
                    current_lines = [line_text]
                    pending_label = None
                    current_type  = "control"
                    current_page  = page_num
                    continue

                # ── C-style clause solo: "C10" alone ──
                if m := ctrl_solo.match(line_text):
                    flush()
                    current_lines  = []
                    pending_label  = m.group(1)
                    pending_ltype  = "control"
                    current_page   = page_num
                    continue

                # ── C-style clause inline: "C10. text..." ──
                if m := ctrl_inline.match(line_text):
                    flush()
                    current_lines = [line_text]
                    pending_label = None
                    current_type  = "control"
                    current_page  = page_num
                    continue

                # ── O-style objective ──
                if m := obj_solo.match(line_text):
                    flush()
                    current_lines  = []
                    pending_label  = m.group(1)
                    pending_ltype  = "objective"
                    current_page   = page_num
                    continue

                if m := obj_inline.match(line_text):
                    flush()
                    current_lines = [line_text]
                    pending_label = None
                    current_type  = "objective"
                    current_page  = page_num
                    continue

                # ── Resolve pending clause label ──
                if pending_label:
                    current_lines = [f"{pending_label}. {line_text}"]
                    current_type  = pending_ltype
                    pending_label = None
                    continue

                # ── Sub-items and notes attach to current clause ──
                if subitem_pat.match(line_text) or note_pat.match(line_text):
                    current_lines.append(line_text)
                    continue

                # ── Bold sub-heading ──
                if is_bold and max_font <= heading_thr:
                    current_lines.append(line_text)
                    continue

                # ── Body text ──
                current_lines.append(line_text)

    flush()
    return chunks


# ── R2 scope filter ──────────────────────────────────────────────────────────

def in_r2_scope(chunk: dict, r2_scope: dict | None) -> bool:
    """Return True if the chunk should be included for R2 extraction."""
    if r2_scope is None:
        return True
    part = chunk.get("part_context", "")
    include = r2_scope.get("include_parts", [])
    exclude = r2_scope.get("exclude_parts", [])
    if include and not any(kw.lower() in part.lower() for kw in include):
        return False
    if any(kw.lower() in part.lower() for kw in exclude):
        return False
    return True


# ── Claude extraction ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a planning compliance expert reading Australian Development Control Plans (DCPs), Local Environmental Plans (LEPs), and State Environmental Planning Policies (SEPPs).

Your job: extract structured numeric planning rules from a single DCP/LEP chunk.

## WHAT TO EXTRACT
Only extract rules with a CLEAR NUMERIC VALUE (metres, %, storeys, ratio, spaces, degrees, m²).
Do NOT extract qualitative rules (e.g. "consistent with streetscape", "to council's satisfaction").
Do NOT invent numbers. Do NOT extract rules about neighbouring properties.
Do NOT extract rules naming a specific street address or heritage item.

## CLAUSE STYLES
Documents use different naming conventions — handle all of them:
- C10, C15a  (Canada Bay / Marrickville Controls)
- O5, O12    (Objectives — skip, don't extract)
- DS3.4      (Inner West Ashfield Design Solutions)
- cl 4.3, clause 4.4  (LEP clauses)
- E2.1, E4   (Canada Bay Part E section numbers)
- PC2.1, AS3.1  (Performance Criteria / Acceptable Solutions)
- Plain numbered: 4.1.6.2

## RULE SCHEMA — every field required

- parameter: MUST be one of the allowed list below
- value: number (not string)
- unit: "m" | "m2" | "mm" | "pct" | "storeys" | "ratio" | "degrees" | "spaces"
- operator: "min" | "max" | "eq"
- zone: "R2" | "R3" | "all_residential" | "all" | "not_specified"
- dwelling_type: "dwelling_house" | "semi_detached" | "dual_occupancy_attached" |
  "dual_occupancy_detached" | "secondary_dwelling" | "outbuilding" |
  "accessory_structure" | "all"
- storey_applicability: "single_storey" | "second_storey" | "all_storeys" | "not_specified"
- lot_type: "single_frontage" | "corner_lot" | "internal_lot" | "rear_lot" | "all" | "not_specified"
- conditions: list of strings — WHEN this rule applies. Be comprehensive:
  include site area thresholds, heritage, flood, zone qualifiers, setback triggers,
  parking area type, lot width conditions, table row conditions, etc.
- exceptions: list of strings — WHEN this rule does NOT apply or may be varied.
  Copy language directly from the text.
- related_rules: list of clause references this rule cross-references.
- source_clause: exact clause label (e.g. "C10", "DS4.3", "cl 4.3")
- routing_path: full path from document to clause. Use the section_path from the chunk
  context and append the clause label and page number.
  Format: "Document Name > Part/Chapter > Section Heading > ClauseLabel p.N"
- source_text_quote: 15–30 word exact quote containing the numeric value
- confidence: 0.0–1.0
  0.9+ = clear value, unambiguous parameter
  0.7–0.9 = some interpretation needed
  <0.7 = uncertain

## ALLOWED PARAMETERS
front_setback, side_setback_ground, side_setback_upper,
rear_setback, rear_setback_upper,
max_height, max_storeys, height_plane, max_wall_height,
landscaped_area_pct, landscaped_area_front_pct,
private_open_space, private_open_space_min_dimension,
fsr, site_coverage_pct,
min_lot_size, min_lot_width, min_dwelling_width,
basement_setback, outbuilding_setback,
front_fence_height_solid, front_fence_height_open,
side_fence_height, rear_fence_height,
parking_spaces_per_dwelling, min_bicycle_spaces,
max_driveway_width, min_parking_space_length, min_parking_space_width,
balcony_rear_setback, building_separation

## TIERED / TABLE RULES
For rules in a table (e.g. site coverage by lot area), produce ONE rule per tier.
Set conditions[] to capture the threshold (e.g. "lot area 201–300m²").

## RETURN FORMAT
{"rules": [...]}
Return valid JSON only. No prose. No markdown fences."""


def extract_rules_from_chunk(chunk: dict, lga: str) -> list[dict]:
    user_msg = f"""Document: {chunk['source_document']}
Section path: {chunk['section_path']}
Part context: {chunk.get('part_context', 'not specified')}
Page: {chunk['page']}
Clause type: {chunk.get('clause_type', 'unknown')}
Numeric values found: {', '.join(chunk['measurements']) or 'none'}

--- CHUNK TEXT ---
{chunk['text']}
--- END CHUNK ---

Extract all numeric planning rules. For routing_path, use:
  "{chunk['section_path']} > <ClauseLabel> p.{chunk['page']}"

Return JSON only."""

    for attempt in range(4):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            break
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                time.sleep(15 * (attempt + 1))
            else:
                raise

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1][4:] if parts[1].startswith("json") else parts[1]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
        rules  = parsed.get("rules", [])
    except json.JSONDecodeError as e:
        print(f"  WARN JSON error {chunk['chunk_id']}: {e} — raw: {raw[:120]}")
        return []

    enriched = []
    for i, r in enumerate(rules):
        enriched.append({
            "rule_id":               f"{chunk['chunk_id']}_r{i+1}",
            "parameter":             r.get("parameter"),
            "value":                 r.get("value"),
            "unit":                  r.get("unit"),
            "operator":              r.get("operator"),
            "zone":                  r.get("zone", "R2"),
            "dwelling_type":         r.get("dwelling_type", "all"),
            "storey_applicability":  r.get("storey_applicability", "not_specified"),
            "lot_type":              r.get("lot_type", "not_specified"),
            "conditions":            r.get("conditions", []),
            "exceptions":            r.get("exceptions", []),
            "related_rules":         r.get("related_rules", []),
            "lga":                   lga,
            "source_document":       chunk["source_document"],
            "source_clause":         r.get("source_clause", ""),
            "source_page":           chunk["page"],
            "source_text":           r.get("source_text_quote", chunk["text"][:150]),
            "routing_path":          r.get("routing_path", chunk["section_path"]),
            "part_context":          chunk.get("part_context", ""),
            "source_type":           "dcp_extraction",
            "superseded_by":         None,
            "confidence":            r.get("confidence", 0.5),
            "extracted_by":          MODEL,
            "verified":              False,
        })
    return enriched


# ── Validation ───────────────────────────────────────────────────────────────

VALID_PARAMS = {
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
VALID_UNITS = {"m", "m2", "mm", "pct", "storeys", "ratio", "degrees", "spaces"}
VALID_OPS   = {"min", "max", "eq"}
VALID_DWT   = {
    "dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
    "semi_detached", "attached_dwelling", "secondary_dwelling",
    "outbuilding", "accessory_structure", "all",
}


def validate(rule: dict) -> list[str]:
    warns = []
    if rule.get("parameter") not in VALID_PARAMS:
        warns.append(f"unknown parameter: {rule.get('parameter')}")
    if rule.get("unit") not in VALID_UNITS:
        warns.append(f"unknown unit: {rule.get('unit')}")
    if rule.get("operator") not in VALID_OPS:
        warns.append(f"unknown operator: {rule.get('operator')}")
    if rule.get("dwelling_type") not in VALID_DWT:
        warns.append(f"unknown dwelling_type: {rule.get('dwelling_type')}")
    if not isinstance(rule.get("value"), (int, float)):
        warns.append(f"value not a number: {rule.get('value')}")
    return warns


def deduplicate(rules: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rules:
        key = (r.get("parameter"), r.get("value"), r.get("unit"),
               r.get("dwelling_type"), r.get("lot_type"), r.get("zone"),
               r.get("storey_applicability"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_lga(lga_cfg: dict, dry_run: bool = False, reuse_chunks: bool = False):
    label  = lga_cfg["label"]
    lga    = lga_cfg["lga"]
    output = BASE_DIR / lga_cfg["output"]

    print(f"\n{'='*65}")
    print(f"LGA: {label}  ({lga})")
    print(f"Output: {output.name}")
    print("="*65)

    # ── Step 1: chunk all PDFs ────────────────────────────────────────
    all_chunks = []
    for pdf_cfg in lga_cfg["pdfs"]:
        pdf_path = BASE_DIR / pdf_cfg["path"]
        chunk_cache = CHUNKS_DIR / f"{label.lower().replace(' ','_')}_{pdf_path.stem}.jsonl"

        if reuse_chunks and chunk_cache.exists():
            chunks = [json.loads(l) for l in open(chunk_cache, encoding="utf-8") if l.strip()]
            print(f"\nLoaded {len(chunks)} cached chunks from {chunk_cache.name}")
        else:
            print(f"\nChunking: {pdf_path.name}")
            chunks = chunk_pdf(pdf_cfg, label)
            with open(chunk_cache, "w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
            print(f"  → {len(chunks)} chunks saved to {chunk_cache.name}")

        # R2 scope filtering
        r2_scope = pdf_cfg.get("r2_scope")
        before = len(chunks)
        chunks = [c for c in chunks if in_r2_scope(c, r2_scope)]
        if r2_scope and before != len(chunks):
            print(f"  R2 scope filter: {before} → {len(chunks)} chunks")

        # Show part distribution
        parts = Counter(c.get("part_context","—") for c in chunks)
        for part, n in parts.most_common(6):
            print(f"    {n:3d} chunks — {part}")

        all_chunks.extend(chunks)

    # Only extract from chunks with numeric values
    extractable = [c for c in all_chunks
                   if c.get("clause_type") in ("control", "body")
                   and c.get("measurements")]
    skipped = len(all_chunks) - len(extractable)
    print(f"\nTotal: {len(all_chunks)} chunks, {len(extractable)} with measurements "
          f"({skipped} skipped — no numbers)")

    if dry_run:
        print("\n[DRY RUN] Sample extractable chunks:")
        for c in extractable[:5]:
            print(f"  {c['chunk_id']}  path={c['section_path']}")
            print(f"    meas={c['measurements']}  text={c['text'][:80]}")
        return []

    # ── Step 2: extract rules ─────────────────────────────────────────
    print(f"\nExtracting rules from {len(extractable)} chunks (parallel, 3 workers)...")
    all_rules = []
    errors    = 0
    done      = 0

    def _call(chunk):
        return chunk, extract_rules_from_chunk(chunk, lga)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_call, c): c for c in extractable}
        for fut in as_completed(futures):
            done += 1
            try:
                chunk, rules = fut.result()
                all_rules.extend(rules)
                summary = ", ".join(f"{r['parameter']}={r['value']}{r['unit']}" for r in rules) \
                          if rules else "0 rules"
                print(f"  [{done:3d}/{len(extractable)}] {chunk['chunk_id']}  {summary}",
                      flush=True)
            except Exception as e:
                errors += 1
                print(f"  ERROR {futures[fut]['chunk_id']}: {e}", flush=True)

    print(f"\nRaw rules: {len(all_rules)}, errors: {errors}")

    # ── Step 3: dedup + validate ──────────────────────────────────────
    unique = deduplicate(all_rules)
    print(f"After dedup: {len(unique)}")

    valid, flagged = [], []
    for r in unique:
        warns = validate(r)
        if warns:
            r["validation_warnings"] = warns
            flagged.append(r)
        else:
            valid.append(r)
    print(f"Valid: {len(valid)}, flagged: {len(flagged)}")
    for r in flagged[:5]:
        print(f"  FLAG {r['rule_id']}: {r.get('validation_warnings')}")

    # ── Step 4: merge LEP rules ───────────────────────────────────────
    lep_path = lga_cfg.get("lep_rules")
    lep_rules = []
    if lep_path and (BASE_DIR / lep_path).exists():
        lep_rules = json.loads((BASE_DIR / lep_path).read_text(encoding="utf-8"))
        print(f"Merging {len(lep_rules)} LEP rules from {lep_path}")

    all_out = lep_rules + valid + flagged

    # ── Step 5: write output ──────────────────────────────────────────
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(all_out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(all_out)} rules → {output}")

    # Parameter summary
    by_param = {}
    for r in valid:
        by_param.setdefault(r["parameter"], []).append(r)
    print(f"\nParameter breakdown ({len(by_param)} unique):")
    for p in sorted(by_param):
        rs = by_param[p]
        vals = ", ".join(f"{r['value']}{r['unit']}" for r in rs[:4])
        print(f"  {p}: {len(rs)} rule(s) — {vals}{'...' if len(rs)>4 else ''}")

    return all_out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lga",          default="",    help="Run only this LGA label")
    parser.add_argument("--dry-run",      action="store_true", help="Chunk only, no API calls")
    parser.add_argument("--reuse-chunks", action="store_true", help="Skip re-chunking if cache exists")
    parser.add_argument("--manifest",     default="pipeline_manifest.json")
    args = parser.parse_args()

    manifest = json.loads((BASE_DIR / args.manifest).read_text(encoding="utf-8"))

    targets = manifest
    if args.lga:
        targets = [e for e in manifest if e["label"].lower() == args.lga.lower()]
        if not targets:
            print(f"LGA '{args.lga}' not found in manifest. "
                  f"Available: {[e['label'] for e in manifest]}")
            sys.exit(1)

    for cfg in targets:
        run_lga(cfg, dry_run=args.dry_run, reuse_chunks=args.reuse_chunks)

    print("\n" + "="*65)
    print("Pipeline complete.")


if __name__ == "__main__":
    main()
