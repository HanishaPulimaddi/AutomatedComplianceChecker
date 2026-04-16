"""
parse_pdf_iw.py — Parse Inner West DCP PDFs into JSONL chunks.

Handles three different section-numbering schemes:
  Marrickville  : 4.1 / 4.1.5 / 4.1.5.3  (numeric dotted, number + title on separate lines)
  Ashfield      : Font-size-based headings (16pt), DS\d+.\d+ design solution labels
  Leichhardt    : C3.1 / C3.2  (C-prefixed dotted, number + title on separate lines)

All three use clause labels for controls/objectives.
Marrickville/Leichhardt: C\d+ / O\d+
Ashfield: DS\d+.\d+ (design solutions)

Usage:
    cd extractor/
    python parse_pdf_iw.py
"""

import fitz
import json
import os
import re
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def get_body_font_size(doc) -> float:
    sizes = []
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes.append(span["size"])
    counts = Counter(round(s) for s in sizes)
    return counts.most_common(1)[0][0] if counts else 10


def extract_measurements(text: str) -> list[str]:
    pattern = re.compile(
        r"(\d+\.?\d*)\s*(mm|m\b|metres?|meters?|%|degrees?|dBA|m2|sqm|storeys?|ratio)",
        re.IGNORECASE,
    )
    return [m[0] + m[1] for m in pattern.findall(text)]


def parse_pdf(
    path: str,
    source_document: str,
    chunk_prefix: str,
    section_pattern: re.Pattern,
    section_solo_pattern: re.Pattern = None,
    section_font_threshold: float = None,
    skip_pages: int = 2,
    extra_control_patterns: list = None,
) -> list[dict]:
    """
    Generic PDF parser. Configurable per DCP via section_pattern.

    section_pattern:       Matches "NUMBER TITLE" on the same line.
                           Must have group(1)=number, group(2)=title.
    section_solo_pattern:  Matches a bare section number alone on its line
                           (e.g. "4.1.5" or "C3.1"). The following bold line
                           is then used as the title. Optional.
    section_font_threshold: If set, any bold line whose font size is >= this
                            value is treated as a section heading, with the
                            entire line text used as both number and title.
                            Used for Ashfield where headings are unnumbered.
    extra_control_patterns: Additional re.Pattern objects to detect clause labels
                            beyond C\d+ / O\d+. Each pattern must have
                            group(1)=label and optional group(2)=rest of line.
    """
    doc = fitz.open(path)
    body_size = get_body_font_size(doc)
    heading_threshold = body_size + 1.5

    chunks = []
    current_section = "Unknown"
    current_section_title = "Unknown"
    current_text_lines: list[str] = []
    current_page = 1
    current_clause_type = "body"
    chunk_counter = 0

    # Standard clause patterns (Marrickville / Leichhardt style)
    control_solo_pattern    = re.compile(r"^(C\d+)\.?\s*$")
    objective_solo_pattern  = re.compile(r"^(O\d+)\.?\s*$")
    control_inline_pattern  = re.compile(r"^(C\d+)\.?\s+(.+)", re.DOTALL)
    objective_inline_pattern = re.compile(r"^(O\d+)\.?\s+(.+)", re.DOTALL)
    subitem_pattern = re.compile(r"^[a-z]\)\s+|^\d+\.\s+|^[ivxIVX]+\.\s+")
    note_pattern    = re.compile(r"^(Note|NB)[\s:]", re.IGNORECASE)

    skip_lines = {
        "Controls", "Objectives", "Objective", "Control", "Background",
        "Marrickville Development Control Plan 2011",
        "Comprehensive Inner West DCP 2016",
        "Inner West Development Control Plan",
        "PLACE", "PART C", "PART 4", "PART 4:  RESIDENTIAL DEVELOPMENT",
        "Chapter F \u2013 Development Category Guidelines",
        "Part 1\u2013 Residential \u2013 Low Density Zone",
        "Low Density Residential Development",
        "Performance Criteria", "Design Solution",
    }

    pending_clause_label  = None
    pending_clause_type   = None
    pending_section_num   = None   # bare section number waiting for its title line

    def flush_chunk():
        nonlocal chunk_counter
        text = " ".join(current_text_lines).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) > 30:
            chunks.append({
                "chunk_id":        f"{chunk_prefix}_{chunk_counter:03d}",
                "text":            text,
                "page":            current_page,
                "section":         current_section,
                "section_title":   current_section_title,
                "clause_type":     current_clause_type,
                "measurements":    extract_measurements(text),
                "source_document": source_document,
            })
            chunk_counter += 1

    for page_num, page in enumerate(doc, start=1):
        if page_num <= skip_pages:
            continue

        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                line_text = ""
                max_font  = 0
                is_bold   = False

                for span in line["spans"]:
                    line_text += span["text"]
                    if span["size"] > max_font:
                        max_font = span["size"]
                    if "Bold" in span.get("font", "") or "bold" in span.get("font", ""):
                        is_bold = True

                line_text = line_text.strip()
                if not line_text:
                    continue

                # ── Skip noise ──
                if re.match(r"^(Version:|Document Set ID:|Page [A-Z]-|Error! Reference)", line_text):
                    continue
                if re.match(r"^Figure [A-Z0-9]", line_text):
                    continue
                if line_text in skip_lines:
                    continue
                if re.match(r"^\d+\s*$", line_text):   # lone page numbers
                    continue
                if re.match(r"^page\s+\d+\s*$", line_text, re.IGNORECASE):
                    continue

                is_large = max_font > heading_threshold

                # ── Font-size-only section heading (Ashfield style) ──────────
                # Any bold line at or above section_font_threshold is a section.
                if section_font_threshold and is_bold and max_font >= section_font_threshold:
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = None
                    pending_section_num  = None
                    current_section       = line_text          # use full text as ID
                    current_section_title = line_text
                    current_page = page_num
                    current_clause_type = "heading"
                    continue

                # ── Pattern-based section heading ────────────────────────────
                # Case 1: number + title on the same line
                section_match = section_pattern.match(line_text)
                if section_match and (is_large or is_bold):
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = None
                    pending_section_num  = None
                    current_section = section_match.group(1)
                    current_section_title = (
                        section_match.group(2).strip()
                        if section_match.lastindex >= 2
                        else ""
                    )
                    current_page = page_num
                    current_clause_type = "heading"
                    continue

                # Case 2: bare section number alone (title on the next bold line)
                if section_solo_pattern:
                    solo_match = section_solo_pattern.match(line_text)
                    if solo_match and (is_large or is_bold):
                        flush_chunk()
                        current_text_lines = []
                        pending_clause_label = None
                        pending_section_num  = solo_match.group(1)
                        current_page = page_num
                        current_clause_type = "heading"
                        continue

                # Case 3: if we have a pending section number, this bold line is its title
                if pending_section_num and (is_bold or is_large):
                    current_section       = pending_section_num
                    current_section_title = line_text
                    pending_section_num   = None
                    # Don't flush again — already flushed when we saw the number
                    continue

                # If a pending section num exists but this line is plain text,
                # commit the number with an empty title and fall through.
                if pending_section_num:
                    current_section       = pending_section_num
                    current_section_title = ""
                    pending_section_num   = None

                # ── Extra clause patterns (e.g. DS\d+.\d+ for Ashfield) ──────
                if extra_control_patterns:
                    matched_extra = False
                    for pat in extra_control_patterns:
                        m = pat.match(line_text)
                        if m:
                            flush_chunk()
                            current_text_lines = [line_text]
                            pending_clause_label = None
                            current_clause_type  = "control"
                            current_page = page_num
                            matched_extra = True
                            break
                    if matched_extra:
                        continue

                # ── Solo clause labels ──
                if m := control_solo_pattern.match(line_text):
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = m.group(1)
                    pending_clause_type  = "control"
                    current_page = page_num
                    continue

                if m := objective_solo_pattern.match(line_text):
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = m.group(1)
                    pending_clause_type  = "objective"
                    current_page = page_num
                    continue

                # ── Inline clause labels ──
                if m := control_inline_pattern.match(line_text):
                    flush_chunk()
                    current_text_lines = [line_text]
                    pending_clause_label = None
                    current_clause_type  = "control"
                    current_page = page_num
                    continue

                if m := objective_inline_pattern.match(line_text):
                    flush_chunk()
                    current_text_lines = [line_text]
                    pending_clause_label = None
                    current_clause_type  = "objective"
                    current_page = page_num
                    continue

                # ── Text following solo label ──
                if pending_clause_label:
                    current_text_lines = [f"{pending_clause_label}. {line_text}"]
                    current_clause_type  = pending_clause_type
                    pending_clause_label = None
                    continue

                # ── Sub-items and notes attach to parent ──
                if subitem_pattern.match(line_text) or note_pattern.match(line_text):
                    current_text_lines.append(line_text)
                    continue

                # ── Bold inline label (sub-heading) ──
                if is_bold and max_font <= heading_threshold:
                    current_text_lines.append(line_text)
                    continue

                # ── Body text ──
                current_text_lines.append(line_text)

    flush_chunk()
    return chunks


# ── Per-PDF configuration ────────────────────────────────────────────────────

CONFIGS = [
    # ── Marrickville Part 4.1 (Residential) ────────────────────────────────
    {
        "path":            str(BASE_DIR / "docs/inner_west_dcp_marrickville_part_4.1.pdf"),
        "source_document": "Marrickville DCP 2011 Part 4.1",
        "chunk_prefix":    "mrk_dcp_4_1",
        "output":          str(BASE_DIR / "data/chunks_marrickville_dcp_4_1.jsonl"),
        # Section number and title appear on SEPARATE bold lines:
        #   "4.1.5"  (14pt bold)
        #   "Streetscape and design"  (14pt bold)
        "section_pattern":      re.compile(r"^(4\.1(?:\.\d+)+)\s+(.+)"),  # inline (fallback)
        "section_solo_pattern": re.compile(r"^(4\.1(?:\.\d+)+)$"),        # bare number
        "skip_pages": 6,
    },

    # ── Marrickville Part 2.10 (Parking) ───────────────────────────────────
    {
        "path":            str(BASE_DIR / "docs/Marrickville DCP 2011 - 2 10 Parking.pdf"),
        "source_document": "Marrickville DCP 2011 Part 2.10 Parking",
        "chunk_prefix":    "mrk_parking",
        "output":          str(BASE_DIR / "data/chunks_marrickville_parking.jsonl"),
        # Same two-line heading pattern as Part 4.1:
        #   "2.10.5"  (14pt bold)  →  "Car parking provision"  (14pt bold)
        "section_pattern":      re.compile(r"^(2\.10(?:\.\d+)*)\s+(.+)"),
        "section_solo_pattern": re.compile(r"^(2\.10(?:\.\d+)*)$"),
        "skip_pages": 4,  # skip cover + ToC
    },

    # ── Marrickville Part 2.11 (Fencing) ───────────────────────────────────
    {
        "path":            str(BASE_DIR / "docs/Marrickville DCP 2011 - 2 11 Fencing (1).pdf"),
        "source_document": "Marrickville DCP 2011 Part 2.11 Fencing",
        "chunk_prefix":    "mrk_fencing",
        "output":          str(BASE_DIR / "data/chunks_marrickville_fencing.jsonl"),
        # Same structure: "2.11.4"  →  "Residential fencing"  on separate lines
        "section_pattern":      re.compile(r"^(2\.11(?:\.\d+)*)\s+(.+)"),
        "section_solo_pattern": re.compile(r"^(2\.11(?:\.\d+)*)$"),
        "skip_pages": 4,
    },

    # ── Ashfield Chapter F (Dwelling Houses) ───────────────────────────────
    {
        "path":            str(BASE_DIR / "docs/inner_west_dcp_Ashfield_chapter_F.pdf"),
        "source_document": "Inner West DCP 2016 Chapter F (Ashfield Dwelling Houses)",
        "chunk_prefix":    "iw_dcp_f",
        "output":          str(BASE_DIR / "data/chunks_ashfield_dcp_f.jsonl"),
        # Ashfield has NO numbered sections. Headings are large bold prose words
        # at 16pt (e.g. "Application", "Scale", "Context", "Setbacks").
        # Design solutions use DS\d+.\d+ labels instead of C/O.
        "section_pattern":        re.compile(r"^(DUMMY_NEVER_MATCHES)$"),  # unused
        "section_font_threshold":  14.0,                                    # 16pt headings
        "extra_control_patterns": [
            re.compile(r"^(DS\d+\.\d+)\.?\s*(.*)"),  # DS1.1, DS3.4 etc.
        ],
        "skip_pages": 2,
    },

]
# Former Leichhardt LGA PDFs excluded — R1 General Residential, outside R2 scope.


def main():
    os.makedirs(str(BASE_DIR / "data"), exist_ok=True)

    for cfg in CONFIGS:
        print(f"\n{'='*60}")
        print(f"Parsing: {cfg['source_document']}")
        print(f"File   : {cfg['path']}")

        chunks = parse_pdf(
            path                   = cfg["path"],
            source_document        = cfg["source_document"],
            chunk_prefix           = cfg["chunk_prefix"],
            section_pattern        = cfg["section_pattern"],
            section_solo_pattern   = cfg.get("section_solo_pattern"),
            section_font_threshold = cfg.get("section_font_threshold"),
            extra_control_patterns = cfg.get("extra_control_patterns"),
            skip_pages             = cfg["skip_pages"],
        )

        # Summary
        sections  = set(c["section"] for c in chunks)
        controls  = [c for c in chunks if c["clause_type"] == "control"]
        with_nums = [c for c in chunks if c["measurements"]]

        print(f"Chunks  : {len(chunks)}  ({len(controls)} controls, {len(with_nums)} with measurements)")
        print(f"Sections: {len(sections)}  {sorted(sections)[:10]}{'...' if len(sections)>10 else ''}")

        # First 3 control chunks with measurements
        sample = [c for c in controls if c["measurements"]][:3]
        for c in sample:
            print(f"  SAMPLE  [{c['chunk_id']}] sec={c['section']}  meas={c['measurements']}")
            print(f"          {c['text'][:120]}")

        # Write JSONL
        with open(cfg["output"], "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c) + "\n")
        print(f"Written : {cfg['output']}")


if __name__ == "__main__":
    main()
