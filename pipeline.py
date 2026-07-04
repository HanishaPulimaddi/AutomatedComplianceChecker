"""
pipeline.py — Generic DCP/LEP rule extraction pipeline.

Reads pipeline_manifest.json. For each LGA entry:
  1. Calls Claude to detect the document's structure (clause labels, section
     heading style, part headers) — works on any council's DCP format
  2. Chunks PDF using detected patterns, embeds full section hierarchy in
     every chunk so the extractor knows its context
  3. Filters chunks to R2-scoped sections (if r2_scope is set in manifest)
  4. Calls Claude to extract rules with full context (conditions, exceptions, routing)
  5. Validates schema, deduplicates, merges LEP rules
  6. Writes output rules JSON

Usage:
    python pipeline.py                          # run all LGAs in manifest
    python pipeline.py --lga "Canada Bay R3"    # run one LGA
    python pipeline.py --lga "Canada Bay R3" --dry-run # chunk + detect only, no extraction
    python pipeline.py --lga "Canada Bay R3" --reuse-chunks  # skip re-chunking if cached
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
from dotenv import load_dotenv

load_dotenv()

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = BASE_DIR / "data"
CHUNKS_DIR = DATA_DIR / "pipeline_chunks"
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_EXTRACT = "gemini-flash-lite-latest"   # rule extraction via Gemini free tier

# Structure detection uses a free-tier model.
# Set GROQ_API_KEY in .env for Groq (Llama 3.3, free tier).
# Set GEMINI_API_KEY in .env for Gemini Flash (free tier).
# One of these must be set — structure detection does not use Anthropic.
_STRUCTURE_PROVIDER = None
_STRUCTURE_CLIENT   = None

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _init_structure_client():
    global _STRUCTURE_PROVIDER, _STRUCTURE_CLIENT

    groq_key   = os.environ.get("GROQ_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if groq_key:
        try:
            from openai import OpenAI as GroqClient
            _STRUCTURE_CLIENT   = GroqClient(api_key=groq_key,
                                             base_url="https://api.groq.com/openai/v1")
            _STRUCTURE_PROVIDER = "groq"
            print("  Structure detection: Groq (free tier) — llama-3.3-70b-versatile")
            return
        except ImportError:
            print("  Groq key found but openai package missing — falling back to Gemini")

    if gemini_key:
        try:
            from google import genai
            _STRUCTURE_CLIENT   = genai.Client(api_key=gemini_key)
            _STRUCTURE_PROVIDER = "gemini"
            print("  Structure detection: Gemini Flash (free tier)")
            return
        except ImportError:
            raise RuntimeError("google-genai package required: pip install google-genai")

    raise RuntimeError(
        "No free model API key found. Set GROQ_API_KEY or GEMINI_API_KEY in .env.\n"
        "  Groq free tier: https://console.groq.com\n"
        "  Gemini free tier: https://aistudio.google.com/app/apikey"
    )


def _call_structure_model(system_prompt: str, user_content: str) -> str:
    if _STRUCTURE_PROVIDER is None:
        _init_structure_client()

    if _STRUCTURE_PROVIDER == "groq":
        resp = _STRUCTURE_CLIENT.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            max_tokens=1000,
            temperature=0,
        )
        return resp.choices[0].message.content.strip()

    # Gemini — try 2.5-flash first, fall back to 2.0-flash-lite on overload
    for model in ("gemini-flash-lite-latest",):
        for attempt in range(3):
            try:
                resp = _STRUCTURE_CLIENT.models.generate_content(
                    model=model,
                    contents=f"{system_prompt}\n\n{user_content}",
                    config={"max_output_tokens": 4096, "temperature": 0},
                )
                return resp.text.strip()
            except Exception as e:
                err = str(e)
                if ("429" in err or "quota" in err.lower()) and attempt < 2:
                    time.sleep(15 * (attempt + 1))
                elif "503" in err and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                elif "503" in err:
                    break  # try next model
                else:
                    raise
    raise RuntimeError("All Gemini models unavailable for structure detection")


def _call_extraction_model(system_prompt: str, user_content: str) -> str:
    """Call Gemini for rule extraction with retry on rate-limit errors."""
    if _STRUCTURE_PROVIDER is None:
        _init_structure_client()

    for attempt in range(4):
        try:
            if _STRUCTURE_PROVIDER == "groq":
                resp = _STRUCTURE_CLIENT.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_content},
                    ],
                    max_tokens=4000,
                    temperature=0,
                )
                return resp.choices[0].message.content.strip()

            # Gemini — try 2.5-flash first, fall back to 2.0-flash-lite on overload
            last_err = None
            for model in ("gemini-flash-lite-latest",):
                try:
                    resp = _STRUCTURE_CLIENT.models.generate_content(
                        model=model,
                        contents=f"{system_prompt}\n\n{user_content}",
                        config={"max_output_tokens": 4096, "temperature": 0},
                    )
                    return resp.text.strip()
                except Exception as me:
                    last_err = me
                    if "503" in str(me):
                        continue  # try next model
                    raise
            raise last_err

        except Exception as e:
            if ("429" in str(e) or "quota" in str(e).lower()) and attempt < 3:
                time.sleep(15 * (attempt + 1))
            elif "503" in str(e) and attempt < 3:
                time.sleep(10 * (attempt + 1))
            else:
                raise

    raise RuntimeError("Extraction model failed after 4 attempts")


# ── LLM document structure detection ────────────────────────────────────────

STRUCTURE_DETECT_PROMPT = """You are analysing an Australian planning document (DCP, LEP, or SEPP) to understand its formatting conventions.

Look at the sample pages provided and identify:

1. **Clause/control labels** — the labels used to number individual rules or controls.
   Examples from different councils:
   - Canada Bay / Marrickville: "C10", "C15a", "O5" (objectives)
   - Inner West Ashfield: "DS3.4", "DS14.2" (Design Solutions)
   - LEPs: "cl 4.3", "clause 4.4"
   - Parramatta: "P1", "P2"
   - Wollongong: "DCP-R1.1"
   - Some DCPs have NO labels — rules are just numbered paragraphs
   Could be anything — look at what's actually in the text.

2. **Objective labels** — separate labels for objectives (not controls).
   Often "O1", "O2" etc. Return null if objectives aren't labelled separately.

3. **Section heading style** — how major sections are titled:
   - "numeric_inline": number and title on same line, e.g. "4.1.5 Setbacks" or "E2.1 Building Height"
   - "numeric_solo": number alone on one line, title on the next bold line
   - "font_size": no numbers — headings are just large/bold prose text like "Building Setbacks"
   - "mixed": both numbered and prose headings present

4. **Section heading regex** — a Python regex to match section headings.
   Group 1 = section number, Group 2 = title (or null if font_size style).
   Leave null if font_size style.

5. **Part header pattern** — text that appears in page running headers to identify
   which Part/Chapter the page belongs to. E.g. "Part 1" or "Chapter F".
   Return the regex that would match it. Group 1 = part number, Group 2 = part title.

6. **Noise lines** — recurring boilerplate lines to skip (council name, document title,
   page number format, etc.)

Return ONLY valid JSON in this exact schema:

{
  "clause_label_regex": "^(C\\d+[a-z]?)\\.?\\s+(.*)",
  "clause_label_type": "control",
  "objective_label_regex": "^(O\\d+)\\.?\\s+(.*)",
  "section_heading_style": "numeric_inline",
  "section_heading_regex": "^([A-Z]?\\d+(?:\\.\\d+)+)\\s+(.+)",
  "part_header_regex": "Part\\s+(\\d+)[\\s\\-\\u2013\\u2014]*([A-Za-z][^\\n]*)?",
  "font_heading_min_size_offset": null,
  "noise_line_patterns": ["^CITY OF CANADA BAY$", "^Canada Bay DCP"],
  "clause_label_examples": ["C10", "C15", "C20a"],
  "notes": "brief description of the document structure"
}

Rules:
- All regex values must be valid Python regex strings (escape backslashes)
- If there are no clause labels, set clause_label_regex to null
- If section headings are font-size only (no numbers), set section_heading_regex to null
  and set font_heading_min_size_offset to the font size offset above body text (e.g. 3.0)
- noise_line_patterns: list of regex strings for boilerplate to skip
- Return ONLY the JSON object, no prose, no markdown"""


def detect_document_structure(pdf_path: str, source_doc: str,
                               skip_pages: int = 0) -> dict:
    """
    Send the first ~8 content pages to Claude and ask it to detect the
    document's clause labeling scheme, section heading style, and noise lines.
    Returns a structure dict used to configure the chunker.
    Caches result alongside chunk files.
    """
    cache_key  = Path(pdf_path).stem + "_structure.json"
    cache_path = CHUNKS_DIR / cache_key
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"  Structure: loaded from cache ({cached.get('notes','')[:60]})")
        return cached

    doc = fitz.open(pdf_path)
    sample_text = []
    pages_sampled = 0
    for page_num, page in enumerate(doc, start=1):
        if page_num <= skip_pages:
            continue
        text = page.get_text("text").strip()
        if text:
            sample_text.append(f"=== PAGE {page_num} ===\n{text}")
            pages_sampled += 1
        if pages_sampled >= 8:
            break

    combined = "\n\n".join(sample_text)[:6000]  # ~6k chars is enough to detect structure

    print(f"  Detecting structure (free model, {pages_sampled} sample pages)...")
    for attempt in range(3):
        try:
            raw = _call_structure_model(
                STRUCTURE_DETECT_PROMPT,
                f"Document: {source_doc}\n\nSample pages:\n\n{combined}",
            )
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(15 * (attempt + 1))
            else:
                raise
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1][4:] if parts[1].startswith("json") else parts[1]
    raw = raw.strip()

    try:
        structure = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARN: could not parse structure response, using fallback")
        print(f"  Raw response (full):\n{raw}")
        structure = _fallback_structure()

    # Validate all regex patterns before caching
    structure = _validate_structure(structure)
    structure["source_document"] = source_doc

    cache_path.write_text(json.dumps(structure, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    print(f"  Structure detected: style={structure['section_heading_style']}, "
          f"clause_ex={structure.get('clause_label_examples',['?'])[:2]}")
    if structure.get("notes"):
        print(f"  Notes: {structure['notes'][:80]}")
    return structure


def _fallback_structure() -> dict:
    """Conservative fallback: assumes C-style clauses, numeric sections."""
    return {
        "clause_label_regex":       r"^(C\d+[a-z]?)\.?\s+(.*)",
        "clause_label_type":        "control",
        "objective_label_regex":    r"^(O\d+)\.?\s+(.*)",
        "section_heading_style":    "numeric_solo",
        "section_heading_regex":    r"^([A-Z]?\d+(?:\.\d+)+)\s+(.+)",
        "part_header_regex":        r"Part\s+(\d+)[\s\-\u2013\u2014]*([A-Za-z][^\n]*)?",
        "font_heading_min_size_offset": None,
        "noise_line_patterns":      [],
        "clause_label_examples":    [],
        "notes":                    "fallback structure",
    }


def _validate_structure(s: dict) -> dict:
    """Test-compile all regex fields; replace invalid ones with safe fallbacks."""
    regex_fields = [
        "clause_label_regex", "objective_label_regex",
        "section_heading_regex", "part_header_regex",
    ]
    for field in regex_fields:
        val = s.get(field)
        if val is None:
            continue
        try:
            re.compile(val)
        except re.error as e:
            print(f"  WARN: invalid regex in {field}: {e} — clearing field")
            s[field] = None

    noise = s.get("noise_line_patterns", [])
    valid_noise = []
    for pat in noise:
        try:
            re.compile(pat)
            valid_noise.append(pat)
        except re.error:
            pass
    s["noise_line_patterns"] = valid_noise
    return s


# ── Page-header Part detection ────────────────────────────────────────────────

def get_page_part(page_text: str, part_header_re) -> str | None:
    """Extract the Part/Chapter name from page header text."""
    if part_header_re is None:
        return None
    for line in page_text.split("\n")[:8]:
        m = part_header_re.search(line.strip())
        if m:
            num   = m.group(1) if m.lastindex >= 1 else ""
            title = (m.group(2) if m.lastindex >= 2 else "") or ""
            title = title.strip().rstrip("\u2013\u2014\u2212-").strip()
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


# ── Table helpers ─────────────────────────────────────────────────────────────

def _is_real_table(rows: list) -> bool:
    """
    Return True if PyMuPDF table rows look like a real data table,
    not a two-column layout artifact.
    """
    if not rows or len(rows) < 2:
        return False
    max_cols = max(len(r) for r in rows)
    if max_cols < 2:
        return False
    total = sum(len(r) for r in rows)
    empty = sum(1 for r in rows for cell in r if not (cell or "").strip())
    if total == 0 or empty / total > 0.6:
        return False
    all_text = " ".join((cell or "") for r in rows for cell in r)
    return bool(re.search(r"\d", all_text))


def _rows_to_markdown(rows: list) -> str:
    """Convert table row list-of-lists to a markdown table string."""
    if not rows:
        return ""
    max_cols = max(len(r) for r in rows)
    padded = [(list(r) + [""] * (max_cols - len(r))) for r in rows]

    def fmt(row):
        return "| " + " | ".join((c or "").replace("|", "/").strip() for c in row) + " |"

    sep = "| " + " | ".join(["---"] * max_cols) + " |"
    return "\n".join([fmt(padded[0]), sep] + [fmt(r) for r in padded[1:]])


# ── Core PDF chunker ─────────────────────────────────────────────────────────

def chunk_pdf(pdf_cfg: dict, lga_label: str, structure: dict) -> list[dict]:
    """
    Parse one PDF into chunks using the LLM-detected document structure.
    Each chunk carries full section hierarchy so extraction has context.
    """
    path       = pdf_cfg["path"]
    source_doc = pdf_cfg["source_document"]
    skip_pages = pdf_cfg.get("skip_pages", 0)

    doc       = fitz.open(path)
    body_size = get_body_font_size(doc)
    heading_thr = body_size + 1.5

    # ── Compile detected patterns ──────────────────────────────────────────
    def _compile(field):
        val = structure.get(field)
        return re.compile(val, re.DOTALL) if val else None

    clause_re  = _compile("clause_label_regex")
    obj_re     = _compile("objective_label_regex")
    section_re = _compile("section_heading_regex")
    part_hdr_re = _compile("part_header_regex")

    # Font-size heading threshold (for Ashfield-style prose headings)
    font_offset = structure.get("font_heading_min_size_offset")
    font_heading_thr = (body_size + font_offset) if font_offset else None

    # Section heading style
    sec_style = structure.get("section_heading_style", "numeric_solo")

    # Noise patterns
    noise_pats = [re.compile(p) for p in structure.get("noise_line_patterns", [])]

    # Always-useful sub-item / note patterns
    subitem_pat = re.compile(r"^[a-z]\)\s+|^\d+\.\s+|^[ivxIVX]+\.\s+")
    note_pat    = re.compile(r"^(Note|NB)[\s:]", re.IGNORECASE)

    # Section heading solo: bare number alone on a line (numeric_solo style)
    # Covers both the detected pattern AND a generic fallback
    sec_solo_re = re.compile(r"^([A-Z]?\d+(?:\.\d+)+)$")

    chunks        = []
    chunk_counter = 0
    current_part  = ""
    current_sec   = ""
    current_title = ""
    current_lines: list[str] = []
    current_page  = 1
    current_type  = "body"
    pending_label = None
    pending_ltype = None
    pending_secnum = None

    def routing_path() -> str:
        parts = [source_doc]
        if current_part:
            parts.append(current_part)
        if current_sec or current_title:
            label = current_sec + (f" {current_title}" if current_title else "")
            parts.append(label.strip())
        return " > ".join(parts)

    def flush():
        nonlocal chunk_counter
        text = re.sub(r"\s+", " ", " ".join(current_lines)).strip()
        if len(text) < 30:
            return
        chunks.append({
            "chunk_id":        f"{lga_label.lower().replace(' ','_')}_{chunk_counter:04d}",
            "source_document": source_doc,
            "part_context":    current_part,
            "section":         current_sec,
            "section_title":   current_title,
            "section_path":    routing_path(),
            "clause_type":     current_type,
            "page":            current_page,
            "text":            text,
            "measurements":    extract_measurements(text),
        })
        chunk_counter += 1

    for page_num, page in enumerate(doc, start=1):
        if page_num <= skip_pages:
            continue

        page_text = page.get_text("text")
        part = get_page_part(page_text, part_hdr_re)
        if part:
            current_part = part

        # ── Table extraction (before text blocks) ──────────────────────────
        table_rects = []
        try:
            for t in page.find_tables().tables:
                rows = t.extract()
                if _is_real_table(rows):
                    table_rects.append(fitz.Rect(t.bbox))
                    md   = _rows_to_markdown(rows)
                    meas = extract_measurements(md)
                    if meas:
                        flush()
                        chunks.append({
                            "chunk_id":        f"{lga_label.lower().replace(' ','_')}_{chunk_counter:04d}",
                            "source_document": source_doc,
                            "part_context":    current_part,
                            "section":         current_sec,
                            "section_title":   current_title,
                            "section_path":    routing_path(),
                            "clause_type":     "table",
                            "page":            page_num,
                            "text":            md,
                            "measurements":    meas,
                        })
                        chunk_counter += 1
                        current_lines = []
        except Exception as _te:
            pass  # find_tables() unsupported on this page type — continue

        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue
            # Skip text blocks that fall inside a real table (already extracted)
            if table_rects:
                block_r = fitz.Rect(block["bbox"])
                if any(block_r.intersects(tr) for tr in table_rects):
                    continue
            for line_obj in block["lines"]:
                line_text = "".join(s["text"] for s in line_obj["spans"]).strip()
                if not line_text:
                    continue

                max_font = max((s["size"] for s in line_obj["spans"]), default=0)
                is_bold  = any("Bold" in s.get("font", "") or "bold" in s.get("font", "")
                               for s in line_obj["spans"])
                is_large = max_font > heading_thr

                # ── Skip noise ─────────────────────────────────────────────
                if any(p.search(line_text) for p in noise_pats):
                    continue
                if re.match(r"^(Version:|Document Set ID:)", line_text):
                    continue
                if re.match(r"^(Figure|Table)\s+[A-Z0-9\-]", line_text):
                    continue
                if re.match(r"^\d+\s*$", line_text) or re.match(r"^[A-Z]-\d+\s*$", line_text):
                    continue
                if re.match(r"^[Pp]age\s+[A-Z]?-?\d+\s*$", line_text):
                    continue

                # ── Font-size-only heading (e.g. Ashfield prose headings) ──
                if font_heading_thr and is_bold and max_font >= font_heading_thr:
                    # Only treat as heading if it's NOT a clause label
                    is_clause = (clause_re and clause_re.match(line_text)) or \
                                (obj_re and obj_re.match(line_text))
                    if not is_clause:
                        flush()
                        current_lines  = []
                        pending_label  = None
                        pending_secnum = None
                        current_title  = line_text
                        current_sec    = ""
                        current_page   = page_num
                        current_type   = "heading"
                        continue

                # ── Numeric section heading on one line: "E2.1 Title" ──────
                if (is_large or is_bold) and section_re:
                    m = section_re.match(line_text)
                    if m:
                        flush()
                        current_lines  = []
                        pending_label  = None
                        pending_secnum = None
                        current_sec    = m.group(1)
                        current_title  = m.group(2).strip() if m.lastindex >= 2 else ""
                        current_page   = page_num
                        current_type   = "heading"
                        continue

                # ── Numeric section number alone (title on next bold line) ──
                if (is_large or is_bold) and sec_solo_re.match(line_text):
                    # Don't confuse clause labels (e.g. DS3.4) with section numbers
                    is_clause = (clause_re and clause_re.match(line_text)) or \
                                (obj_re and obj_re.match(line_text))
                    if not is_clause:
                        flush()
                        current_lines  = []
                        pending_label  = None
                        pending_secnum = sec_solo_re.match(line_text).group(1)
                        current_page   = page_num
                        current_type   = "heading"
                        continue

                # ── Resolve pending section number ─────────────────────────
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

                # ── Clause label ───────────────────────────────────────────
                if clause_re:
                    m = clause_re.match(line_text)
                    if m:
                        flush()
                        label = m.group(1)
                        rest  = m.group(2).strip() if m.lastindex >= 2 else ""
                        if rest:
                            current_lines = [line_text]
                            pending_label = None
                        else:
                            current_lines  = []
                            pending_label  = label
                            pending_ltype  = "control"
                        current_type = "control"
                        current_page = page_num
                        continue

                # ── Objective label ────────────────────────────────────────
                if obj_re:
                    m = obj_re.match(line_text)
                    if m:
                        flush()
                        label = m.group(1)
                        rest  = m.group(2).strip() if m.lastindex >= 2 else ""
                        if rest:
                            current_lines = [line_text]
                            pending_label = None
                        else:
                            current_lines  = []
                            pending_label  = label
                            pending_ltype  = "objective"
                        current_type = "objective"
                        current_page = page_num
                        continue

                # ── Resolve pending clause label ───────────────────────────
                if pending_label:
                    current_lines = [f"{pending_label}. {line_text}"]
                    current_type  = pending_ltype
                    pending_label = None
                    continue

                # ── Sub-items and notes attach to current chunk ────────────
                if subitem_pat.match(line_text) or note_pat.match(line_text):
                    current_lines.append(line_text)
                    continue

                # ── Bold sub-heading (smaller than heading threshold) ──────
                if is_bold and max_font <= heading_thr:
                    current_lines.append(line_text)
                    continue

                # ── Body text ──────────────────────────────────────────────
                current_lines.append(line_text)

    flush()
    return chunks


def _resplit_by_clause(chunks: list[dict], clause_re_str: str | None) -> list[dict]:
    """
    Post-process chunks to fix 2-column table layouts (e.g. Ashfield's
    Performance Criteria | Design Solutions format).

    When PyMuPDF merges both columns into one block, multiple clause labels
    (DS1.1, DS1.2, C10, C15...) land inside a single chunk.  This function
    finds those large merged chunks and re-splits them at each clause label
    boundary so that downstream extraction gets one focused chunk per clause.

    Works for any DCP format — generic on the detected clause_label_regex.
    """
    if not clause_re_str:
        return chunks

    # Extract just the label pattern from the full regex
    # e.g. "^(DS\d+\.\d+)\s+(.*)" → label_pat matches "DS1.1", "DS3.4" etc.
    m = re.match(r'^\^?\((.+?)\)', clause_re_str)
    if not m:
        return chunks
    label_pat = re.compile(r'(?<!\w)(' + m.group(1) + r')(?=[\s\.\:])', re.IGNORECASE)

    result = []
    split_count = 0

    for chunk in chunks:
        text = chunk.get("text", "")
        matches = list(label_pat.finditer(text))

        # Only re-split if 2+ clause labels are present in the same chunk
        if len(matches) < 2:
            result.append(chunk)
            continue

        # Prepend any text before the first clause label as a non-split header
        pre = text[:matches[0].start()].strip()
        if len(pre) >= 30:
            pre_chunk = dict(chunk)
            pre_chunk["text"] = pre
            pre_chunk["chunk_id"] = f"{chunk['chunk_id']}_pre"
            pre_chunk["measurements"] = extract_measurements(pre)
            result.append(pre_chunk)

        for i, match in enumerate(matches):
            start = match.start()
            end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sub   = text[start:end].strip()
            if len(sub) < 15:
                continue
            sub_chunk = dict(chunk)
            sub_chunk["text"]         = sub
            sub_chunk["chunk_id"]     = f"{chunk['chunk_id']}_s{i:02d}"
            sub_chunk["measurements"] = extract_measurements(sub)
            sub_chunk["clause_type"]  = "control"
            result.append(sub_chunk)
            split_count += 1

    if split_count > 0:
        print(f"  Re-split {split_count} merged clause chunks (2-column layout fix)")
    return result


# ── R2 scope filter ──────────────────────────────────────────────────────────

def in_r2_scope(chunk: dict, r2_scope: dict | None) -> bool:
    if r2_scope is None:
        return True
    part    = chunk.get("part_context", "") + " " + chunk.get("section_path", "")
    include = r2_scope.get("include_parts", [])
    exclude = r2_scope.get("exclude_parts", [])
    def _part_matches(kw: str, part: str) -> bool:
        import re as _re
        return bool(_re.search(r'\b' + _re.escape(kw.lower()) + r'\b', part.lower()))

    if include and not any(_part_matches(kw, part) for kw in include):
        return False
    if any(_part_matches(kw, part) for kw in exclude):
        return False
    return True


# ── Claude rule extraction ────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a planning compliance expert reading Australian Development Control Plans (DCPs), Local Environmental Plans (LEPs), and State Environmental Planning Policies (SEPPs).

Extract structured numeric planning rules from the chunk of DCP/LEP text provided.

## WHAT TO EXTRACT
- Only rules with a CLEAR NUMERIC VALUE (metres, %, storeys, ratio, spaces, degrees, m²)
- Do NOT extract qualitative rules ("consistent with streetscape", "to council's satisfaction")
- Do NOT invent numbers or extract rules about neighbouring properties
- Do NOT extract site-specific rules naming a particular address or heritage item

## DO NOT FORCE-FIT
Every parameter in the schema below means a SPECIFIC type of control. A number that is not
that type of control does not belong in that parameter, even if it is the closest-sounding name.
Examples of force-fitting to REJECT:
  - A basement driveway entry clearance height is NOT max_height (max_height = height of the building).
  - A flood planning level (m AHD) is NOT max_height.
  - A waste-chute storey-count threshold is NOT max_storeys.
  - A foreshore public access strip width is NOT rear_setback (rear_setback = dwelling-to-boundary distance).
  - A bin-storage-room floor area is NOT private_open_space (private_open_space = outdoor amenity space per dwelling).
  - A tree canopy spread in m² is NOT landscaped_area_pct (landscaped_area_pct = % of site area landscaped).
If a number's control type has no matching parameter in the schema below, DO NOT extract it at all.
Skipping a rule is always correct; misfiling it under the wrong parameter is always wrong.

## CLAUSE LABELS — any format is valid
The document may use C10, DS3.4, P1, DCP-R1.1, cl 4.3, or any other scheme.
Use whatever label the document actually uses in source_clause.

## RULE SCHEMA (all fields required)

parameter — one of:
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
  balcony_rear_setback, building_separation,

  adaptable_housing_pct           — % of dwellings in a development required to be adaptable-housing standard. unit=pct
  min_habitable_floor_level       — flood-affected land only: minimum floor/basement level in metres AHD (Australian Height Datum). unit=m
  topography_cut_fill_max         — maximum permitted cut or fill alteration to natural ground level. unit=m
  waste_bin_walking_distance_max  — max walking distance from a dwelling to communal bin storage / kerbside presentation point. unit=m
  waste_vehicle_clearance_height  — min vertical clearance for waste collection vehicle access (driveway/basement). unit=m
  waste_vehicle_access_width_min  — min driveway width for waste collection vehicle access. unit=m
  protected_tree_min_height       — height at/above which a tree is a protected tree. unit=m
  protected_tree_min_trunk_diameter — trunk diameter at ground level at/above which a tree is protected (even if under the height threshold). unit=mm
  protected_tree_min_canopy_spread  — canopy spread at/above which a tree is protected regardless of height. unit=m
  tree_setback_from_dwelling      — min distance a tree must be planted from an approved dwelling/retaining wall. unit=m
  tree_replacement_ratio          — number of replacement trees required per tree removed. unit=ratio
  solar_access_hours_min          — min hours of direct sunlight required to neighbours' north-facing windows / private open space (state the reference date/window in conditions[]). unit=hours
  foreshore_public_access_width   — min width of public access strip required between mean high water mark and a foreshore building. unit=m
  seawall_height_max              — max height a permitted seawall may protrude above mean high water mark. unit=m

  pool_coping_height_max          — foreshore setback only: max height of a pool/spa edge above natural ground. unit=m
  fence_exclusion_zone_from_water — foreshore only: distance from mean high water mark within which boundary fences are not permitted. unit=m
  retaining_wall_height_max       — foreshore only: max retaining wall height. unit=m
  ramp_crest_level_max_drop       — max drop of a basement/driveway ramp crest below natural ground level within a setback. unit=m
  driveway_landscape_strip_width_min — min width of landscaped strip between a driveway/access-handle and a side boundary. unit=m
  outbuilding_floor_area_max      — max total floor area of outbuildings on a lot. unit=m2
  flood_parking_level_offset      — flood-affected land: required min/max offset of a parking/driveway surface level relative to a named flood level (state which flood level, e.g. 1%AEP or 5%AEP, in conditions[]). unit=m
  flood_tailwater_level           — reference flood tailwater level from a council flood study table (informational input, not itself a floor-level requirement). unit=m
  concessional_development_addition_max — max size of a once-only concessional/exempt addition to an existing dwelling (extract both the % and m2 forms as separate rules if both given). unit=pct or m2
  garage_frontage_occupancy_max   — max % of the site frontage a garage/parking structure may occupy. unit=pct
  garage_structure_width_max      — max width of a garage or other parking structure (distinct from max_driveway_width, which is the driveway itself). unit=m
  waste_bin_carting_route_width_min — min width of the bin carting route from bin storage to the collection/holding point. unit=m
  exempt_tree_species_height_max  — height below which specific listed tree species are exempt from protection (distinct from protected_tree_min_height, the general threshold). unit=m
  tree_canopy_target_pct          — min % of site area (or LGA) required to be tree canopy — distinct from landscaped_area_pct, which is general soft landscaping. unit=pct
  hardstand_front_setback_min     — min distance from the front boundary to the dwelling, specifically for the hardstand-parking exception when a garage/carport cannot be sited at side/rear. unit=m
  secondary_facade_offset         — min setback of a secondary building facade from the primary building facade (not a boundary setback). unit=m
  secondary_facade_max_width_pct  — max width of a secondary building facade as a % of total site frontage (distinct from secondary_facade_offset, which is a setback distance). unit=pct
  primary_facade_width_pct        — max width of the primary (front) building facade as a % of total site frontage. unit=pct
  roof_pitch_min                  — min roof pitch angle. unit=degrees
  roof_pitch_max                  — max roof pitch angle. unit=degrees
  max_driveway_width_pct          — max driveway width as a % of the site's frontage width (distinct from max_driveway_width, an absolute distance). unit=pct
  privacy_sill_height             — min window sill height (floor to sill) required on a side elevation for privacy. unit=m
  balcony_side_setback            — min setback of an upper-level rear balcony from a side boundary (distinct from side_setback_upper, which covers the whole upper floor, not just balconies). unit=m
  landscaping_strip_width         — min width of a continuous landscaped strip on the street side of a front fence. unit=m
  landscaped_area_rear_pct        — min % of the rear yard area required to be landscaped (distinct from landscaped_area_pct, the whole-site figure, and landscaped_area_front_pct). unit=pct
  pool_coping_boundary_setback_min — min distance from a swimming pool/spa coping to the property boundary (distinct from pool_coping_height_max, a height limit). unit=m
  dormer_height_max               — max dormer height, base to ridge. unit=m
  dwelling_massing_offset_max     — max distance one dwelling may project into the rear yard beyond an adjoining dwelling on the same lot (e.g. dual occupancy). unit=m
  landscape_planting_min_mature_height — required min mature height for new screen/native plantings (not a protection threshold for existing trees). unit=m
  pathway_boundary_setback        — min distance a pathway or driveway must be from a common/site boundary. unit=m
  fence_boundary_setback          — min setback of a fence from a boundary (e.g. for sightline/visibility reasons), distinct from fence height parameters. unit=m
  satellite_dish_height_max       — max height of a satellite dish above ground in a rear yard. unit=m
  height_plane_ground_tolerance_max — max allowance for one point/side of a building to exceed the height plane due to ground undulation. unit=m
  deck_patio_height_max           — max height of a ground floor deck/patio above natural ground level. unit=m

value         — number (not string)
unit          — "m" | "m2" | "mm" | "pct" | "storeys" | "ratio" | "degrees" | "spaces" | "hours"
operator      — "min" | "max" | "eq"
zone          — "R2" | "R3" | "all_residential" | "all" | "not_specified"
dwelling_type — "dwelling_house" | "semi_detached" | "dual_occupancy_attached" |
                "dual_occupancy_detached" | "secondary_dwelling" | "outbuilding" |
                "accessory_structure" | "all"
storey_applicability — "single_storey" | "second_storey" | "all_storeys" | "not_specified"
lot_type      — "single_frontage" | "corner_lot" | "internal_lot" | "rear_lot" | "all" | "not_specified"

conditions    — list of strings: WHEN this rule applies
                Be comprehensive: site area thresholds, heritage status, flood overlay,
                zone qualifiers, lot width conditions, table row conditions, parking area type, etc.

exceptions    — list of strings: WHEN this rule may be varied or does NOT apply
                Copy language directly from the text.

related_rules — list of clause references this rule cross-references

source_clause — exact clause label as it appears in the document (e.g. "C10", "DS4.3", "P1")

routing_path  — full path from document to this clause:
                "<source_document> > <part_context> > <section> > <source_clause> p.<page>"
                Use the section_path provided in the chunk context.

source_text_quote — 15–30 word exact quote containing the numeric value

confidence    — 0.0–1.0
                0.9+ = clear value, unambiguous parameter
                0.7–0.9 = some interpretation needed
                <0.7 = uncertain (return anyway, backend filters by confidence)

## TIERED / TABLE RULES
Produce ONE rule per tier. Put the threshold in conditions[].
E.g. site_coverage table: one rule per lot-area row.

## FENCE HEIGHT RULES
When a fence height clause applies to a front fence without distinguishing solid vs open,
extract it TWICE — once as front_fence_height_solid and once as front_fence_height_open.
When a clause explicitly states separate heights for solid and open fences, extract each separately.

## UNIT CONVERSION
Always express setback and height values in METRES (m), not millimetres.
Convert: 900mm → value=0.9, unit="m". 1500mm → value=1.5, unit="m".
Only use unit="mm" if the value is a tolerance or precision spec, not a dimensional control.

## RETURN FORMAT
{"rules": [...]}
Return valid JSON only. No prose. No markdown fences."""


BATCH_SIZE = 5   # chunks per API call — reduces calls ~5x vs one-per-chunk


def _format_chunk_for_batch(chunk: dict, idx: int) -> str:
    ctype = chunk.get("clause_type", "unknown")
    type_note = " [MARKDOWN TABLE — extract one rule per data row]" if ctype == "table" else ""
    return (
        f"### CHUNK {idx} — {chunk['chunk_id']}\n"
        f"Section path: {chunk['section_path']}\n"
        f"Part context: {chunk.get('part_context', 'not specified')}\n"
        f"Page: {chunk['page']}  |  Clause type: {ctype}{type_note}\n"
        f"Numeric values: {', '.join(chunk['measurements']) or 'none'}\n"
        f"Text: {chunk['text']}\n"
    )


def _normalize_param(p) -> str | None:
    if not isinstance(p, str):
        return p
    return p.strip().lower().replace(" ", "_").replace("-", "_")


_UNIT_ALIASES: dict[str, str] = {
    # metres
    "metres": "m", "meters": "m", "metre": "m", "meter": "m",
    # square metres
    "m²": "m2", "sqm": "m2", "sq m": "m2", "sq.m": "m2",
    "square metres": "m2", "square meters": "m2", "m^2": "m2",
    # millimetres
    "millimetres": "mm", "millimeters": "mm", "millimetre": "mm", "millimeter": "mm",
    # percent
    "%": "pct", "percent": "pct", "percentage": "pct",
    # storeys
    "storey": "storeys", "stories": "storeys", "story": "storeys", "floors": "storeys",
    # spaces (parking)
    "space": "spaces", "car spaces": "spaces", "car space": "spaces",
}

_OP_ALIASES: dict[str, str] = {
    "minimum": "min", "min.": "min", "at least": "min", ">=": "min", "≥": "min",
    "no less than": "min", "not less than": "min",
    "maximum": "max", "max.": "max", "at most": "max", "<=": "max", "≤": "max",
    "no more than": "max", "not more than": "max", "not exceed": "max",
    "equal": "eq", "equal to": "eq", "exactly": "eq", "=": "eq",
}


def _normalize_unit(u) -> str | None:
    if not isinstance(u, str):
        return u
    return _UNIT_ALIASES.get(u.strip().lower(), u.strip().lower())


def _normalize_op(o) -> str | None:
    if not isinstance(o, str):
        return o
    return _OP_ALIASES.get(o.strip().lower(), o.strip().lower())


# Parameters always expressed in metres — auto-convert mm values
_METRE_PARAMS = {
    "front_setback", "side_setback_ground", "side_setback_upper",
    "rear_setback", "rear_setback_upper", "basement_setback",
    "outbuilding_setback", "balcony_rear_setback", "building_separation",
    "max_height", "max_wall_height", "height_plane",
    "front_fence_height_solid", "front_fence_height_open",
    "side_fence_height", "rear_fence_height",
    "private_open_space_min_dimension", "min_lot_width", "min_dwelling_width",
    "max_driveway_width", "min_parking_space_length", "min_parking_space_width",
}


def _enrich_rule(r: dict, chunk: dict, lga: str, rule_idx: int) -> dict:
    param = _normalize_param(r.get("parameter"))
    value = r.get("value")
    unit  = _normalize_unit(r.get("unit"))
    # Auto-convert mm → m for dimensional parameters
    if unit == "mm" and param in _METRE_PARAMS and isinstance(value, (int, float)):
        value = round(value / 1000, 4)
        unit  = "m"
    return {
        "rule_id":               f"{chunk['chunk_id']}_r{rule_idx}",
        "parameter":             param,
        "value":                 value,
        "unit":                  unit,
        "operator":              _normalize_op(r.get("operator")),
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
        "extracted_by":          MODEL_EXTRACT,
        "verified":              False,
    }


def extract_rules_from_batch(chunks: list[dict], lga: str) -> list[dict]:
    """
    Extract rules from up to BATCH_SIZE chunks in a single API call.
    Response format: {"batches": [{"chunk_id": "...", "rules": [...]}, ...]}
    """
    batch_prompt = "\n\n".join(
        _format_chunk_for_batch(c, i + 1) for i, c in enumerate(chunks)
    )

    batch_schema = (
        "Return a JSON object with a 'batches' array — one entry per chunk:\n"
        '{"batches": [{"chunk_id": "<chunk_id>", "rules": [...]}, ...]}\n'
        "Use the exact chunk_id shown in each ### CHUNK header.\n"
        "If a chunk has no numeric rules, return an empty rules array for it.\n"
        "Return valid JSON only."
    )

    user_msg = (
        f"Document: {chunks[0]['source_document']}\n\n"
        f"{batch_prompt}\n\n"
        f"{batch_schema}"
    )

    raw = _call_extraction_model(EXTRACTION_PROMPT, user_msg)
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1][4:] if parts[1].startswith("json") else parts[1]
    raw = raw.strip()

    # Build chunk_id → chunk lookup
    chunk_map = {c["chunk_id"]: c for c in chunks}

    try:
        parsed  = json.loads(raw)
        batches = parsed.get("batches", [])
    except json.JSONDecodeError as e:
        ids = [c["chunk_id"] for c in chunks]
        print(f"  WARN batch JSON error ({ids}): {e} — {raw[:80]}")
        return []

    all_rules = []
    for entry in batches:
        cid   = entry.get("chunk_id", "")
        chunk = chunk_map.get(cid)
        if chunk is None:
            # Try fuzzy match — model sometimes truncates chunk_id
            chunk = next((c for c in chunks if cid in c["chunk_id"] or c["chunk_id"] in cid), None)
        if chunk is None:
            print(f"  WARN: unmatched chunk_id '{cid}' in batch response")
            continue
        for i, r in enumerate(entry.get("rules", []), start=1):
            all_rules.append(_enrich_rule(r, chunk, lga, i))

    return all_rules


# ── Validation & dedup ────────────────────────────────────────────────────────

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
    "adaptable_housing_pct", "min_habitable_floor_level", "topography_cut_fill_max",
    "waste_bin_walking_distance_max", "waste_vehicle_clearance_height",
    "waste_vehicle_access_width_min",
    "protected_tree_min_height", "protected_tree_min_trunk_diameter",
    "protected_tree_min_canopy_spread", "tree_setback_from_dwelling",
    "tree_replacement_ratio", "solar_access_hours_min",
    "foreshore_public_access_width", "seawall_height_max",
    "pool_coping_height_max", "fence_exclusion_zone_from_water",
    "retaining_wall_height_max", "ramp_crest_level_max_drop",
    "driveway_landscape_strip_width_min", "outbuilding_floor_area_max",
    "flood_parking_level_offset", "flood_tailwater_level",
    "concessional_development_addition_max", "garage_frontage_occupancy_max",
    "garage_structure_width_max", "waste_bin_carting_route_width_min",
    "exempt_tree_species_height_max", "tree_canopy_target_pct",
    "hardstand_front_setback_min", "secondary_facade_offset", "secondary_facade_max_width_pct",
    "primary_facade_width_pct", "roof_pitch_min", "roof_pitch_max", "max_driveway_width_pct",
    "privacy_sill_height", "balcony_side_setback", "landscaping_strip_width",
    "landscaped_area_rear_pct", "pool_coping_boundary_setback_min",
    "dormer_height_max", "dwelling_massing_offset_max",
    "landscape_planting_min_mature_height", "pathway_boundary_setback",
    "fence_boundary_setback", "satellite_dish_height_max",
    "height_plane_ground_tolerance_max", "deck_patio_height_max",
    "upper_level_setback_above_four_storeys", "facade_articulation_zone_depth",
    "min_floor_to_ceiling_height", "five_dock_town_centre_height_tier_reference",
    "dwelling_mix_studio_1bed_min_pct", "dwelling_mix_3bed_plus_min_pct",
    "design_excellence_height_trigger", "competitive_design_process_height_trigger",
    "acid_sulfate_soils_class_reference", "affordable_housing_levy_pct",
    "residential_exclusion_buffer_from_road",
    "heritage_cut_fill_max", "heritage_pavilion_addition_separation_min",
    "communal_open_space_min_pct", "communal_open_space_solar_access_pct",
    "communal_open_space_min_area_per_dwelling", "communal_open_space_min_dimension",
    "deep_soil_zone_min_pct", "deep_soil_zone_min_dimension",
    "adg_boundary_separation_habitable", "adg_boundary_separation_non_habitable",
    "apartment_solar_access_pct_min", "apartment_no_solar_access_pct_max",
    "natural_cross_ventilation_pct_min", "cross_through_apartment_depth_max",
    "apartment_min_internal_area", "habitable_room_window_glass_area_min_pct",
    "open_plan_habitable_room_depth_max", "bedroom_min_area", "bedroom_min_dimension",
    "living_room_min_width", "cross_through_apartment_width_min",
    "adg_balcony_min_area", "adg_balcony_min_depth",
    "adg_ground_floor_pos_min_area", "adg_ground_floor_pos_min_depth",
    "apartment_storage_min_volume",
    "secondary_dwelling_max_floor_area", "secondary_dwelling_min_site_area",
    "secondary_dwelling_complying_dev_min_lot_size", "adg_prevails_over_dcp_matters",
    "tod_max_height_floor_rfb", "tod_max_height_floor_ilu_shoptop", "tod_max_fsr_floor",
    "tod_min_lot_width", "tod_affordable_housing_pct",
    "tod_affordable_housing_parking_1bed", "tod_affordable_housing_parking_2bed",
    "tod_affordable_housing_parking_3bed_plus",
    # Codes SEPP 2008 Part 3 (Housing Code) — complying development pathway,
    # kept under a "cdc_" prefix and never merged with DA/DCP-pathway params
    # (e.g. cdc_side_setback_min is a different standard to side_setback_ground,
    # from a different consent pathway with no discretion to vary).
    "cdc_articulation_zone_max_depth", "cdc_articulation_zone_max_element_area_pct",
    "cdc_attached_balcony_max_area_elevated", "cdc_attached_balcony_max_floor_level",
    "cdc_attached_garage_min_width_for_front_access", "cdc_basement_max_area",
    "cdc_battle_axe_front_setback_min", "cdc_battle_axe_min_dimensions",
    "cdc_boundary_wall_max_height", "cdc_boundary_wall_max_length",
    "cdc_classified_road_setback_min", "cdc_corner_lot_min_primary_frontage",
    "cdc_detached_boundary_wall_max_height", "cdc_detached_deck_max_floor_level",
    "cdc_detached_max_gfa", "cdc_detached_max_height",
    "cdc_detached_parallel_road_setback_min", "cdc_detached_rear_setback_min",
    "cdc_detached_side_setback_min", "cdc_detached_studio_max_gfa",
    "cdc_detached_studio_max_height", "cdc_detached_studio_setback_min",
    "cdc_excavation_max_depth", "cdc_excavation_max_depth_acid_sulfate_or_waterbody",
    "cdc_fence_max_height_behind_building_line", "cdc_fence_max_height_forward_of_building_line",
    "cdc_fill_max_height_dwelling", "cdc_fill_max_height_other",
    "cdc_garage_carport_secondary_road_setback_min", "cdc_garage_carport_setback_min",
    "cdc_landscaped_area_min_dimension", "cdc_max_building_height",
    "cdc_max_garage_door_width", "cdc_max_gfa", "cdc_max_gfa_pct_plus_constant_reference",
    "cdc_min_landscaped_area_pct", "cdc_min_lot_area", "cdc_min_lot_width",
    "cdc_min_parking_spaces", "cdc_min_principal_pos_area",
    "cdc_parallel_road_setback_min", "cdc_parking_setback_primary_road_min",
    "cdc_pool_boundary_setback_min", "cdc_pool_coping_max_height",
    "cdc_pool_decking_max_height", "cdc_pool_pump_boundary_setback_min",
    "cdc_primary_road_setback_min", "cdc_principal_pos_min_dimension",
    "cdc_privacy_screen_height_min", "cdc_privacy_screen_trigger_setback",
    "cdc_protected_tree_setback_min", "cdc_public_reserve_setback_min",
    "cdc_rear_setback_min", "cdc_retaining_wall_min_separation",
    "cdc_secondary_road_articulation_min_length_pct", "cdc_secondary_road_setback_min",
    "cdc_secondary_road_window_min_area", "cdc_side_setback_min",
    "cdc_wall_within_boundary_trigger",
    # Codes SEPP 2008 Part 3B (Low Rise Housing Diversity Code) — dual
    # occupancies, manor houses and multi dwelling housing (terraces) under
    # the same CDC pathway; "cdc3b_" prefix keeps it distinct from Part 3's
    # dwelling-house-only "cdc_" params (e.g. side setback minimums differ).
    "cdc3b_articulation_zone_max_depth_primary", "cdc3b_articulation_zone_max_depth_secondary",
    "cdc3b_articulation_zone_max_element_area_pct", "cdc3b_attached_balcony_max_area_elevated",
    "cdc3b_attached_balcony_max_floor_level", "cdc3b_attached_balcony_setback_min",
    "cdc3b_classified_road_setback_min", "cdc3b_detached_boundary_wall_max_height",
    "cdc3b_detached_boundary_wall_max_length", "cdc3b_detached_cabana_shed_rear_setback_min",
    "cdc3b_detached_deck_max_floor_level", "cdc3b_detached_deck_rear_setback_min",
    "cdc3b_detached_max_gfa_side_by_side", "cdc3b_detached_max_gfa_stacked",
    "cdc3b_detached_max_height", "cdc3b_detached_min_lot_area", "cdc3b_detached_min_lot_width",
    "cdc3b_detached_parallel_road_setback_min", "cdc3b_detached_rear_setback_min_side_by_side",
    "cdc3b_detached_rear_setback_min_stacked", "cdc3b_detached_side_setback_min",
    "cdc3b_detached_studio_max_gfa", "cdc3b_detached_studio_max_height",
    "cdc3b_detached_studio_min_separation_from_dwelling", "cdc3b_detached_studio_rear_setback_min",
    "cdc3b_detached_studio_side_setback_min", "cdc3b_excavation_max_depth",
    "cdc3b_excavation_max_depth_acid_sulfate_or_waterbody", "cdc3b_fence_max_height_behind_building_line",
    "cdc3b_fence_max_height_forward_of_building_line", "cdc3b_fill_max_height_dual_occ_manor",
    "cdc3b_fill_max_height_other", "cdc3b_garage_carport_rear_setback_min",
    "cdc3b_garage_carport_secondary_road_setback_min", "cdc3b_garage_min_separation_from_dwelling",
    "cdc3b_geotechnical_report_trigger_depth", "cdc3b_manor_house_faces_road",
    "cdc3b_max_building_height", "cdc3b_max_garage_door_width", "cdc3b_max_garage_door_width_primary",
    "cdc3b_max_garage_door_width_secondary", "cdc3b_max_gfa", "cdc3b_max_gfa_pct",
    "cdc3b_max_gfa_pct_plus_constant", "cdc3b_max_gfa_pct_plus_constant_reference",
    "cdc3b_min_dwelling_separation", "cdc3b_min_dwelling_width",
    "cdc3b_min_landscaped_area_pct_minus_constant", "cdc3b_min_landscaped_area_pct_subdivided",
    "cdc3b_min_landscaped_area_pct_unsubdivided", "cdc3b_min_lot_area", "cdc3b_min_lot_width",
    "cdc3b_min_lot_width_rear_access", "cdc3b_min_parking_spaces_per_dwelling",
    "cdc3b_min_terrace_width", "cdc3b_parallel_road_setback_min", "cdc3b_parking_setback_min",
    "cdc3b_pool_boundary_setback_min", "cdc3b_pool_coping_max_height", "cdc3b_pool_max_fill_height",
    "cdc3b_primary_road_setback_min", "cdc3b_principal_pos_min_area", "cdc3b_protected_tree_setback_min",
    "cdc3b_public_reserve_setback_min", "cdc3b_rear_setback_min", "cdc3b_retaining_wall_landscape_strip_min",
    "cdc3b_retaining_wall_min_separation", "cdc3b_secondary_road_setback_min", "cdc3b_side_setback_min",
    # Canada Bay DCP Part D (Boarding Houses) and Part J (Child Care Centres)
    "boarding_room_min_area_single", "boarding_room_min_area_double", "boarding_room_max_area",
    "boarding_room_max_occupancy", "boarding_house_kitchen_min_area",
    "boarding_house_laundry_circulation_min_width", "boarding_house_social_impact_assessment_trigger",
    "boarding_house_bicycle_parking_per_lodger",
    "childcare_parking_spaces_per_licensed_places", "childcare_max_sign_area", "childcare_setback_note",
}
VALID_UNITS = {"m", "m2", "m3", "mm", "pct", "storeys", "ratio", "degrees", "spaces", "hours", "class", "count"}
VALID_OPS   = {"min", "max", "eq"}
VALID_DWT   = {
    "dwelling_house", "dual_occupancy_attached", "dual_occupancy_detached",
    "semi_detached", "attached_dwelling", "secondary_dwelling",
    "outbuilding", "accessory_structure", "all",
    # Codes SEPP 2008 Part 3B (Low Rise Housing Diversity Code) building
    # types — kept distinct from dual_occupancy_attached/detached above
    # since those describe physical joinery (DCP sense), not the SEPP's
    # side-by-side vs stacked-unit distinction.
    "manor_house", "dual_occupancy_stacked", "multi_dwelling_terraces",
    # Canada Bay DCP Part F's own dwelling-type category, distinct from
    # multi_dwelling_terraces (the Codes SEPP/Part 3B building type).
    "multi_dwelling_housing",
    # Canada Bay DCP Part D — boarding houses (Housing SEPP-driven; permitted
    # in R1/R3/R4/E1/MU1, not R2 except under a narrow walking-distance test).
    "boarding_house", "child_care_centre",
}


def validate(rule: dict) -> list[str]:
    w = []
    if rule.get("parameter") not in VALID_PARAMS:
        w.append(f"unknown parameter: {rule.get('parameter')}")
    if rule.get("unit") not in VALID_UNITS:
        w.append(f"unknown unit: {rule.get('unit')}")
    if rule.get("operator") not in VALID_OPS:
        w.append(f"unknown operator: {rule.get('operator')}")
    if rule.get("dwelling_type") not in VALID_DWT:
        w.append(f"unknown dwelling_type: {rule.get('dwelling_type')}")
    if not isinstance(rule.get("value"), (int, float)):
        w.append(f"value not a number: {rule.get('value')}")
    return w


def deduplicate(rules: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rules:
        key = (r.get("parameter"), r.get("value"), r.get("unit"),
               r.get("dwelling_type"), r.get("lot_type"),
               r.get("zone"), r.get("storey_applicability"),
               r.get("operator"), r.get("source_clause"))
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

    all_chunks = []

    for pdf_cfg in lga_cfg["pdfs"]:
        pdf_path    = BASE_DIR / pdf_cfg["path"]
        chunk_cache = CHUNKS_DIR / f"{label.lower().replace(' ','_')}_{pdf_path.stem}.jsonl"

        # ── Step 1: detect document structure ──────────────────────────
        structure = detect_document_structure(
            str(pdf_path),
            pdf_cfg["source_document"],
            pdf_cfg.get("skip_pages", 0),
        )

        # ── Step 2: chunk PDF ───────────────────────────────────────────
        if reuse_chunks and chunk_cache.exists():
            chunks = [json.loads(l) for l in open(chunk_cache, encoding="utf-8") if l.strip()]
            print(f"  Loaded {len(chunks)} cached chunks — {pdf_path.name}")
        else:
            print(f"\n  Chunking: {pdf_path.name}")
            chunks = chunk_pdf(pdf_cfg, label, structure)
            chunks = _resplit_by_clause(chunks, structure.get("clause_label_regex"))
            with open(chunk_cache, "w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
            print(f"  {len(chunks)} chunks → {chunk_cache.name}")

        # ── Step 3: R2 scope filter ─────────────────────────────────────
        r2_scope = pdf_cfg.get("r2_scope")
        before   = len(chunks)
        chunks   = [c for c in chunks if in_r2_scope(c, r2_scope)]
        if r2_scope and before != len(chunks):
            print(f"  R2 scope: {before} → {len(chunks)} chunks")

        parts = Counter(c.get("part_context", "—") for c in chunks)
        for part, n in parts.most_common(6):
            print(f"    {n:3d}  {part or '(no part detected)'}")

        all_chunks.extend(chunks)

    extractable = [c for c in all_chunks
                   if c.get("clause_type") in ("control", "body", "table", "heading")
                   and c.get("measurements")]
    print(f"\nTotal: {len(all_chunks)} chunks, "
          f"{len(extractable)} extractable (have numeric values)")

    if dry_run:
        print("\n[DRY RUN] Sample chunks with measurements:")
        for c in extractable[:6]:
            print(f"  {c['chunk_id']}  path={c['section_path']}")
            print(f"    meas={c['measurements']}  text={c['text'][:80]}")
        return []

    # ── Step 4: extract rules (batched — BATCH_SIZE chunks per API call) ───
    batches     = [extractable[i:i+BATCH_SIZE] for i in range(0, len(extractable), BATCH_SIZE)]
    n_calls     = len(batches)
    print(f"\nExtracting from {len(extractable)} chunks in {n_calls} batched calls "
          f"(batch_size={BATCH_SIZE}, 3 parallel workers)...")
    all_rules, errors, done = [], 0, 0

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(extract_rules_from_batch, b, lga): b for b in batches}
        for fut in as_completed(futures):
            done += 1
            batch = futures[fut]
            try:
                rules = fut.result()
                all_rules.extend(rules)
                ids  = [c["chunk_id"] for c in batch]
                summ = f"{len(rules)} rules from {len(batch)} chunks"
                print(f"  [{done:3d}/{n_calls}] {ids[0]}…  {summ}", flush=True)
            except Exception as e:
                errors += 1
                print(f"  ERROR batch {done}: {e}", flush=True)

    print(f"\nRaw: {len(all_rules)}, errors: {errors}")

    # ── Step 5: dedup + validate ────────────────────────────────────────
    unique = deduplicate(all_rules)
    print(f"After dedup: {len(unique)}")

    valid, flagged = [], []
    for r in unique:
        w = validate(r)
        if w:
            r["validation_warnings"] = w
            flagged.append(r)
        else:
            valid.append(r)
    print(f"Valid: {len(valid)}, flagged: {len(flagged)}")
    for r in flagged[:5]:
        print(f"  FLAG {r['rule_id']}: {r.get('validation_warnings')}")

    # ── Step 6: prepend LEP rules ───────────────────────────────────────
    lep_path  = lga_cfg.get("lep_rules")
    lep_rules = []
    if lep_path and (BASE_DIR / lep_path).exists():
        lep_rules = json.loads((BASE_DIR / lep_path).read_text(encoding="utf-8"))
        print(f"Prepending {len(lep_rules)} LEP rules")

    all_out = lep_rules + valid + flagged

    # ── Step 7: write ───────────────────────────────────────────────────
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(all_out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(all_out)} rules → {output}")

    by_param = {}
    for r in valid:
        by_param.setdefault(r["parameter"], []).append(r)
    print(f"\nParameter breakdown ({len(by_param)} unique):")
    for p in sorted(by_param):
        rs = by_param[p]
        vals = ", ".join(f"{r['value']}{r['unit']}" for r in rs[:4])
        print(f"  {p}: {len(rs)} — {vals}{'...' if len(rs)>4 else ''}")

    return all_out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lga",          default="")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--reuse-chunks", action="store_true")
    parser.add_argument("--manifest",     default="pipeline_manifest.json")
    args = parser.parse_args()

    manifest = json.loads((BASE_DIR / args.manifest).read_text(encoding="utf-8"))
    targets  = manifest
    if args.lga:
        targets = [e for e in manifest if e["label"].lower() == args.lga.lower()]
        if not targets:
            labels = [e["label"] for e in manifest]
            print(f"'{args.lga}' not in manifest. Available: {labels}")
            sys.exit(1)

    for cfg in targets:
        run_lga(cfg, dry_run=args.dry_run, reuse_chunks=args.reuse_chunks)

    print("\n" + "="*65)
    print("Done.")


if __name__ == "__main__":
    main()
