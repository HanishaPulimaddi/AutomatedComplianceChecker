#!/usr/bin/env python3
import fitz
import json
import re
import os
import sys
import argparse
from collections import Counter
from pathlib import Path

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
    if not font_sizes:
        return 12
    counts = Counter(round(s) for s in font_sizes)
    return counts.most_common(1)[0][0]

def extract_numbers(text):
    pattern = re.compile(r'(\d+\.?\d*)\s*(mm|m|metres|meters|%|degrees|dBA|m2|sqm)', re.IGNORECASE)
    matches = pattern.findall(text)
    return [m[0] + m[1] for m in matches]

def parse_pdf(path: str, part_letter: str = "e", doc_name: str = None) -> list:
    doc = fitz.open(path)
    if doc_name is None:
        pdf_name = Path(path).stem
        doc_name = pdf_name.replace('_', ' ').title()
    
    body_size = get_font_stats(doc)
    heading_threshold = body_size + 1.5
    chunks = []
    current_section = "Unknown"
    current_section_title = "Unknown"
    current_text_lines = []
    current_page = 1
    chunk_counter = 0
    current_clause_type = "body"
    detected_pattern_name = None
    section_pattern = None
    
    # Generic section pattern that matches any format
    generic_section = re.compile(r'^([A-ZE0-9\.\s]+?)\s{2,}(.+)$')
    control_solo_pattern = re.compile(r'^(C\d+)\.?\s*$')
    objective_solo_pattern = re.compile(r'^(O\d+)\.?\s*$')
    subitem_pattern = re.compile(r'^[a-f]\)\s+')
    note_pattern = re.compile(r'^Note[\s:]', re.IGNORECASE)
    figure_pattern = re.compile(r'^Figure', re.IGNORECASE)

    pending_clause_label = None
    pending_clause_type = None
    skip_lines = {"Controls", "Objectives", "Objective", "Development Control Plan"}

    def flush_chunk():
        nonlocal chunk_counter
        text = " ".join(current_text_lines).strip()
        text = re.sub(r'\s+', ' ', text)
        if len(text) > 30:
            numbers = extract_numbers(text)
            chunks.append({
                "chunk_id": f"chunk_{chunk_counter:04d}",
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
                if not line_text or line_text in skip_lines:
                    continue
                if re.match(r'^(Version:|Document Set ID:|Page)', line_text):
                    continue

                # Section heading detection
                if (max_font_size > heading_threshold or is_bold) and re.match(r'^[A-Z0-9E]', line_text):
                    flush_chunk()
                    current_text_lines = []
                    pending_clause_label = None
                    parts = line_text.split(None, 1)
                    current_section = parts[0]
                    current_section_title = parts[1] if len(parts) > 1 else ""
                    current_page = page_num
                    current_clause_type = "heading"
                    continue

                # Solo clause
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

                if pending_clause_label:
                    current_text_lines = [f"{pending_clause_label}. {line_text}"]
                    current_clause_type = pending_clause_type
                    pending_clause_label = None
                    continue

                if subitem_pattern.match(line_text) or note_pattern.match(line_text):
                    current_text_lines.append(line_text)
                    continue

                if is_bold and max_font_size <= heading_threshold:
                    current_text_lines.append(line_text)
                    continue

                current_text_lines.append(line_text)

    flush_chunk()
    return chunks

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal DCP PDF Parser")
    parser.add_argument('pdf_path', help='Path to PDF file')
    parser.add_argument('--doc-name', help='Custom document name')
    parser.add_argument('--part-letter', default='e', help='Part letter')
    args = parser.parse_args()
    
    if not os.path.exists(args.pdf_path):
        print(f"ERROR: File not found: {args.pdf_path}")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(args.pdf_path))[0]
    output_path = f"../data/chunks_{base_name}.jsonl"

    print(f"Parsing {args.pdf_path}...")
    chunks = parse_pdf(args.pdf_path, part_letter=args.part_letter, doc_name=args.doc_name)
    print(f"✓ Extracted {len(chunks)} chunks → {output_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")
            