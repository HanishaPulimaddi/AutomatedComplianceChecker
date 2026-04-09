import fitz
import json
import re
import os
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


def parse_pdf(path: str) -> list[dict]:
    doc = fitz.open(path)

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
    # Section headings like E4.2 — must start with E + digit
    section_pattern = re.compile(r'^(E\d+(\.\d+)?)\s+(.*)')
    # Sub-items a) b) c)
    subitem_pattern = re.compile(r'^[a-f]\)\s+')
    # Notes
    note_pattern = re.compile(r'^Note[\s:]', re.IGNORECASE)
    # Figure captions
    figure_pattern = re.compile(r'^Figure E\d+')

    # Pending solo clause label waiting for text on next line
    pending_clause_label = None
    pending_clause_type = None

    # Lines to skip entirely
    skip_lines = {
        "Controls", "Objectives", "Objective",
        "CITY OF CANADA BAY", "Development Control Plan",
        "Part E", "Single Dwellings, Semi-Detached Dwellings, "
                  "Dual Occupancies and Secondary Dwellings"
    }

    def flush_chunk():
        nonlocal chunk_counter
        text = " ".join(current_text_lines).strip()
        text = re.sub(r'\s+', ' ', text)
        if len(text) > 30:
            numbers = extract_numbers(text)
            chunks.append({
                "chunk_id": f"cb_dcp_e_{chunk_counter:03d}",
                "text": text,
                "page": current_page,
                "section": current_section,
                "section_title": current_section_title,
                "clause_type": current_clause_type,
                "measurements": numbers,
                "source_document": "Canada Bay DCP Part E"
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

                # ── Skip noise lines ──
                if re.match(r'^(Version:|Document Set ID:|Page E-)', line_text):
                    continue
                if figure_pattern.match(line_text):
                    continue
                if line_text in skip_lines:
                    continue
                # Skip lines that are just repeated header text
                if re.match(r'^(Single Dwellings|Semi-Detached|Dual Occupanc)', line_text):
                    continue

                # ── Section heading e.g. "E4.2  Building setbacks" ──
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

                # ── Solo clause label on its own line e.g. "C1." ──
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

                # ── Inline clause e.g. "C1. The front setback..." ──
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

                # ── Text following a pending solo label ──
                if pending_clause_label:
                    current_text_lines = [f"{pending_clause_label}. {line_text}"]
                    current_clause_type = pending_clause_type
                    pending_clause_label = None
                    continue

                # ── Sub-items and notes attach to parent ──
                if subitem_pattern.match(line_text) or note_pattern.match(line_text):
                    current_text_lines.append(line_text)
                    continue

                # ── Sub-section labels like "Front setbacks - primary street" ──
                # These are bold labels inside a section, not new E-numbered sections
                # Attach to body, don't split
                if is_bold and max_font_size <= heading_threshold:
                    current_text_lines.append(line_text)
                    continue

                # ── Body text ──
                current_text_lines.append(line_text)

    flush_chunk()
    return chunks