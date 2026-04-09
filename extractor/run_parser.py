import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from parse_pdf import parse_pdf

chunks = parse_pdf("docs/canada_bay_dcp_part_e.pdf")
print(f"Total chunks: {len(chunks)}")

os.makedirs("data", exist_ok=True)
with open("data/chunks_canada_bay_dcp_part_e.jsonl", "w", encoding="utf-8") as f:
    for c in chunks:
        f.write(json.dumps(c) + "\n")

print("Written to data/chunks_canada_bay_dcp_part_e.jsonl")