import fitz
import json
import re
import os
import sys
from collections import Counter


def get_font_stats(doc):
    font_sizes = []
    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        font_sizes.append(span["size"])
    counts = Counter(round(s) for s in font_sizes)
    body_size = counts.most_common(1)[0][0]
    return body_size


def extract_numbers(text):
    """Only extract numbers that have units — filters out clause number noise."""
    pattern = re.compile(
        r'(\d+\.?\d*)\s*(mm|m|metres|meters|%|degrees|dBA|m2|sqm)',
        re.IGNORECASE
    )
    matches = pattern.findall(text)
    return [m[0] + m[1] for m in matches]


def parse_pdf(path: str, part_letter: str = "e", doc_name: str = None) -> list[dict]:
    """
    Parse a Canada Bay DCP Part into chunks.
    part_letter: 'e' for Part E, 'c' for Part C, etc.
    doc_name: e.g. "Canada Bay DCP Part C"
    """
    doc = fitz.open(path)

    if doc_name is None:
        doc_name = f"Canada Bay DCP Part {part_letter.upper()}"

    part_upper = part_letter.upper()

    body_size = get_font_stats(doc)
    heading_threshold = body_size + 1.5

    chunks = []
    current_section = "Unknown"
    current_section_title = "Unknown"
    current_text_lines = []
    current_page = 1
    chunk_counter = 0
    current_clause_type = "body"

    # "C1." or "C1" alone on a line
    control_solo_pattern = re.compile(r'^(C\d+)\.?\s*$')
    # "O1." alone on a line
    objective_solo_pattern = re.compile(r'^(O\d+)\.?\s*$')
    # "C1. Some text..." on same line
    control_inline_pattern = re.compile(r'^(C\d+)\.?\s+(.+)', re.DOTALL)
    # "O1. Some text..." on same line
    objective_inline_pattern = re.compile(r'^(O\d+)\.?\s+(.+)', re.DOTALL)
    # Section headings like E4.2 or C2.1 - must start with part letter + digit
    section_pattern = re.compile(rf'^({part_upper}\d+(\.\d+)?)\s+(.*)')
    # Sub-items a) b) c)
    subitem_pattern = re.compile(r'^[a-f]\)\s+')
    # Notes
    note_pattern = re.compile(r'^Note[\s:]', re.IGNORECASE)
    # Figure captions
    figure_pattern = re.compile(rf'^Figure {part_upper}\d+')

    # Pending solo clause label waiting for text on next line
    pending_clause_label = None
    pending_clause_type = None

    # Lines to skip entirely
    skip_lines = {
        "Controls", "Objectives", "Objective",
        "CITY OF CANADA BAY", "Development Control Plan",
        f"Part {part_upper}"
    }

    def flush_chunk():
        nonlocal chunk_counter
        text = " ".join(current_text_lines).strip()
        text = re.sub(r'\s+', ' ', text)
        if len(text) > 30:
            numbers = extract_numbers(text)
            chunks.append({
                "chunk_id": f"cb_dcp_{part_letter}_{chunk_counter:03d}",
                "text": text,
                "page": current_page,
                "section": current_section,
                "section_title": current_section_title,
                "clause_type": current_clause_type,
                "measurements": numbers,
                "source_document": doc_name
            })
            chunk_counter += 1

    for page_num, page in enumerate(doc, start=1):

        # Skip table of contents pages
        if page_num <= 2:
            continue

        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                line_text = ""
                max_font_size = 0
                is_bold = False

                for span in line["spans"]:
                    line_text += span["text"]
                    if span["size"] > max_font_size:
                        max_font_size = span["size"]
                    if "Bold" in span["font"] or "bold" in span["font"]:
                        is_bold = True

                line_text = line_text.strip()
                if not line_text:
                    continue

                # -- Skip noise lines --
                if re.match(rf'^(Version:|Document Set ID:|Page {part_upper}-)', line_text):
                    continue
                if figure_pattern.match(line_text):
                    continue
                if line_text in skip_lines:
                    continue

                # -- Section heading e.g. "E4.2  Building setbacks" or "C2.1 ..." --
                section_match = section_pattern.match(line_text)
                if section_match and (max_font_size > heading_threshold or is_bold):
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = None
                    current_section = section_match.group(1)
                    current_section_title = section_match.group(3).strip()
                    current_page = page_num
                    current_clause_type = "heading"
                    continue

                # -- Solo clause label on its own line e.g. "C1." --
                control_solo = control_solo_pattern.match(line_text)
                if control_solo:
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = control_solo.group(1)
                    pending_clause_type = "control"
                    current_page = page_num
                    continue

                objective_solo = objective_solo_pattern.match(line_text)
                if objective_solo:
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = objective_solo.group(1)
                    pending_clause_type = "objective"
                    current_page = page_num
                    continue

                # -- Inline clause e.g. "C1. The front setback..." --
                control_inline = control_inline_pattern.match(line_text)
                if control_inline:
                    flush_chunk()
                    current_text_lines = [line_text]
                    pending_clause_label = None
                    current_clause_type = "control"
                    current_page = page_num
                    continue

                objective_inline = objective_inline_pattern.match(line_text)
                if objective_inline:
                    flush_chunk()
                    current_text_lines = [line_text]
                    pending_clause_label = None
                    current_clause_type = "objective"
                    current_page = page_num
                    continue

                # -- Text following a pending solo label --
                if pending_clause_label:
                    current_text_lines = [f"{pending_clause_label}. {line_text}"]
                    current_clause_type = pending_clause_type
                    pending_clause_label = None
                    continue

                # -- Sub-items and notes attach to parent --
                if subitem_pattern.match(line_text) or note_pattern.match(line_text):
                    current_text_lines.append(line_text)
                    continue

                # -- Sub-section labels (bold but not numbered sections) --
                # Attach to body, don't split
                if is_bold and max_font_size <= heading_threshold:
                    current_text_lines.append(line_text)
                    continue

                # -- Body text --
                current_text_lines.append(line_text)

    flush_chunk()
    return chunks


if __name__ == "__main__":
    # Default: run on Part C if no arg, otherwise use the arg letter
    part = sys.argv[1].lower() if len(sys.argv) > 1 else "c"

    path = f"../docs/canada_bay_dcp_part_{part}.pdf"
    output_path = f"../data/chunks_canada_bay_dcp_part_{part}.jsonl"

    if not os.path.exists(path):
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    print(f"Parsing {path}...")
    chunks = parse_pdf(path, part_letter=part)
    print(f"Extracted {len(chunks)} chunks")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")

    print(f"Saved to {output_path}")

    # Preview first 3 chunks
    print("\n--- Preview (first 3 chunks) ---")
    for c in chunks[:3]:
        print(f"\n{c['chunk_id']} - {c['section']} ({c['section_title']})".encode('ascii', 'replace').decode())
        print(f"  Page {c['page']}: {c['text'][:200]}...".encode('ascii', 'replace').decode())

    # Section breakdown
    sections = {}
    for c in chunks:
        s = c["section"]
        sections[s] = sections.get(s, 0) + 1

    print(f"\n--- Sections found ({len(sections)}) ---")
    for section, count in sorted(sections.items()):
        print(f"  {section}: {count} chunks")