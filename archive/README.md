# Archive

Files here are not loaded by the live API or referenced by any code — kept for reference, not deleted, in case they're useful later.

- **`data/`** — 9 rule files (ADG, Codes SEPP 2008 Housing/LRHDC, DCP Part D/J, heritage, LEP 2013, special precincts, tree species) that were superseded when their content was merged into the hand-tuned `data/rules_r2_canada_bay.json` and `data/rules_r3_canada_bay_pipeline.json`. Editing these files has no effect on the running app — the live rules are in the two files above.
- **`backend/merge_rules.py`** — never imported by `main.py`, `pipeline.py`, or `check_lmr.py`; superseded by the merge logic already baked into `main.py`'s rule-loading.
