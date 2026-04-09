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


def parse_pdf(path: str) -> list[dict]:
    doc = fitz.open(path)

    body_size = get_font_stats(doc)
    heading_threshold = body_size + 1.5

    chunks = []
    current_section = "Unknown"
    current_subsection = "Unknown"
    current_text_lines = []
    current_page = 1
    chunk_counter = 0
    just_saw_heading = False

    clause_pattern = re.compile(
        r'^(C\d+|O\d+|\d+\.\d+|\(\w\)|\d+\)|•|–|—)\s'
    )

    rule_keywords = [
        'must', 'shall', 'must not', 'should',
        'is required', 'are required',
        'minimum', 'maximum', 'not exceed',
        'is prohibited'
    ]

    def looks_like_rule(text):
        return any(kw in text.lower() for kw in rule_keywords)

    def is_new_chunk_boundary(line_text, is_bold, max_font_size):
        if clause_pattern.match(line_text):
            return True
        if just_saw_heading and is_bold and looks_like_rule(line_text):
            return True
        if just_saw_heading and looks_like_rule(line_text):
            return True
        return False

    def flush_chunk():
        nonlocal chunk_counter
        text = " ".join(current_text_lines).strip()
        if len(text) > 50:
            chunks.append({
                "chunk_id": f"cb_dcp_e_{chunk_counter:03d}",
                "text": text,
                "page": current_page,
                "section": current_section,
                "subsection": current_subsection,
                "source_document": "Canada Bay DCP Part E"
            })
            chunk_counter += 1

    for page_num, page in enumerate(doc, start=1):
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

                if max_font_size > heading_threshold and (is_bold or len(line_text) < 80):
                    flush_chunk()
                    current_text_lines = []
                    current_section = line_text
                    current_page = page_num
                    just_saw_heading = True

                elif is_new_chunk_boundary(line_text, is_bold, max_font_size):
                    flush_chunk()
                    current_text_lines = [line_text]
                    current_page = page_num
                    just_saw_heading = False

                else:
                    current_text_lines.append(line_text)
                    just_saw_heading = False

    flush_chunk()
    return chunks