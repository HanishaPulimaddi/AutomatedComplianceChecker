# Automated Compliance Checker

Give it a residential street address in **City of Canada Bay Council**, and it returns the buildable envelope — setbacks, height, floor space — computed from the actual DCP/LEP/SEPP rules for that lot, with every number linked back to the source clause and PDF page.

## What it does

- **Address → buildable envelope.** Resolves an address to its lot polygon and zone via NSW Planning APIs, then computes the buildable footprint from the applicable Council rules.
- **Cited, not just numbers.** Every value (e.g. `front_setback = 4.5m`) links to the DCP clause and PDF page it came from.
- **CDC eligibility screening.** Checks fast-track Complying Development Certificate eligibility (Codes SEPP 2008), as a preliminary screen alongside the standard envelope.
- **LMR pre-flight check.** Screens eligibility under the Housing SEPP 2021 Low and Mid Rise reforms. API-only for now — no frontend UI yet.
- **Map UI.** A React + Leaflet frontend for entering an address and viewing the lot, envelope, and applicable rules.
- **Rhino/Grasshopper export.** A Grasshopper plugin (`RhinoPlugInV1.gh`) turns the same lot/envelope data into real Rhino geometry for massing studies.
- **Rule extraction pipeline.** `pipeline.py` uses LLMs to read DCP/LEP PDFs and extract structured, cited rules — built to generalize beyond Canada Bay, though only Canada Bay's output is loaded into the live API today.

## Scope

**Canada Bay only.** Every other council returns a 404 — this used to also cover Inner West (Marrickville, Ashfield), but that data and pipeline support were deliberately removed to focus on shipping one council well.

Superseded rule files and unused code live in `archive/`, not in `data/`/`backend/` — see `archive/README.md`. Nothing in `archive/` is loaded by the running app.

## Running it

**Backend**
```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```
Runs at `http://localhost:8000`. No API keys needed just to run against the already-extracted rules in `data/`.

**Frontend**
```bash
cd frontend
npm install
npm run dev
```
Runs at `http://localhost:5173`, proxying API calls to the backend.

**Grasshopper plugin**
Open `RhinoPlugInV1.gh` in Rhino with the backend running locally.

**Rule extraction** (only needed to re-extract or add council data — set `GROQ_API_KEY` or `GEMINI_API_KEY` in `.env` first)
```bash
python pipeline.py
```

**Tests**
```bash
python test_accuracy.py          # rule-extraction accuracy against ground truth
python test_accuracy.py --skip-geometry   # skip the envelope-geometry check
```

**Ground truth verification** (interactive CLI for building/updating `data/ground_truth_addresses.json`)
```bash
python verify_ground_truth.py
python verify_ground_truth.py --all        # include already-verified addresses
python verify_ground_truth.py --lga "Canada Bay"
```

## Known limitations

- Only Canada Bay is live; other councils return a 404.
- The envelope shown is a ground-floor footprint — it doesn't yet model upper-storey setbacks or the DCP's height plane.
- On lots much wider than they are deep, or with complex/curved boundaries, the front/rear/side edge detection can be unreliable (the app warns when it detects this, but the underlying detection isn't fixed).
- LMR and CDC checks are preliminary screens, not determinations — several statutory exclusions aren't verified.
- Tiered rules (e.g. site coverage %) are returned as all tiers; the caller must pick the applicable one.

See `ACCURACY.md` for detailed extraction accuracy figures.
