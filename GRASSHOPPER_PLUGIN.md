# Grasshopper / Rhino Plugin

**Owner: Hanisha Pulimaddi.** This is the 3D modelling side of the project — it takes the
same compliance data the web app shows and brings it directly into Rhino, where architects
already do massing studies, instead of asking them to cross-reference a browser tab while
modelling.

## What it does

- Calls the FastAPI backend for a given address (`/envelope` and `/lmr`) — the same backend
  and the same computed values the web UI uses, not a separate calculation.
- Converts the backend's GeoJSON lot and envelope polygons into real Rhino curves.
- Extrudes the buildable envelope into a 3D mass up to the applicable height limit, so an
  architect gets an actual volume to model against, not just a 2D outline.
- Surfaces the rule citations, LMR (Low/Mid-Rise) eligibility, and key site metrics (lot area,
  footprint, height, FSR) as labelled outputs directly in the Grasshopper canvas.

**Core design principle: the plugin consumes backend-computed values — it never recomputes
them.** Every number an architect sees in Rhino traces back to the same `compute_envelope()`
call the API itself serves. This was a deliberate response to an early bug (see below) where
the plugin tried to independently recalculate area from converted Rhino geometry and silently
got it wrong.

## Files

| File | Responsibility |
|---|---|
| `RhinoPlugInV1.gh` | The actual Grasshopper definition — a Python Script component wired to run against a typed address. This is the file you open in Rhino. |
| `grasshopper_plugin.py` | A readable, standalone copy of the code embedded in that component. Grasshopper's own Python component isn't independently runnable or diffable outside Rhino, so this file exists purely so the logic can be read, edited, and reviewed like normal source code. **Editing this file does not update `RhinoPlugInV1.gh`** — the updated code has to be manually pasted back into the component inside Rhino. |

## Inputs / outputs

| I/O | Name | Description |
|---|---|---|
| Input | `x` | Address string |
| Input | `y` | Boolean — set `True` to run |
| Output | `a` | Lot boundary curve |
| Output | `b` | Buildable envelope curve (setback-clipped) |
| Output | `c` | 3D mass — the envelope extruded to the applicable height limit |
| Output | `d` | Rule citations, as plain text (`parameter: value unit` per line) |
| Output | `e` | LMR eligibility summary (zone, eligible, status) |
| Output | `f` | Key site metrics (lot area, envelope footprint, height, FSR) |

## How to run it

1. Start the backend on the default port: `uvicorn backend.main:app --reload --port 8000`
   (see [backend/README.md](backend/README.md)). The plugin calls `http://localhost:8000`
   directly — this is currently **hardcoded**, not configurable from the Grasshopper side.
2. Open `RhinoPlugInV1.gh` in Rhino via the Grasshopper plug-in.
3. Type an address into input `x`.
4. Toggle input `y` to `True` to run.
5. Read the lot/envelope curves and 3D mass directly in the Rhino viewport, and the citation/
   LMR/metrics text from the panel outputs.

## Known issues and lessons learned

Pulled from the project's engineering log — these are real bugs that were found and fixed
during development, kept here so the reasoning isn't lost:

- **Envelope area returning 0 in Grasshopper.** The backend correctly computed a real area
  (e.g. 141.1 m²), but Grasshopper displayed 0.0 and the 3D mass had no volume. Root cause: the
  plugin was trying to recompute area itself from the converted Rhino surface via
  `AreaMassProperties.Compute()`, which returned 0 for that surface — and the backend's own
  `development_controls` (which had the correct number) wasn't even being returned to the
  plugin at the time. Fixed by having the API return `development_controls`, and by having the
  plugin consume the backend's value directly instead of recalculating — which is the origin
  of the "consume, don't recompute" design principle above.
- **Grasshopper/Rhino document-context switching.** A script that tried to assign Rhino layers
  failed with *"this type of object is not supported in Grasshopper"* — it was trying to create
  Rhino-native layers while Grasshopper's own scripting context (`sc.doc`) still pointed at the
  Grasshopper document, not Rhino's. Fixed by explicitly saving `sc.doc`, switching it to
  `Rhino.RhinoDoc.ActiveDoc` for the duration of any Rhino-native operation, then restoring it.
  Grasshopper and Rhino are separate document contexts, and anything touching Rhino-native
  objects from inside a GH component has to switch contexts explicitly.
- **No prior Rhino/Grasshopper experience on the team.** Unlike the domain-expertise gap that
  later drove the council-scope pivot (there were at least two people bridging software and
  architecture at that point), nobody had used Rhino or Grasshopper before this project, and no
  one was available to consult who had. Basic mechanics — the canvas, component wiring, how a
  Python component actually executes inside a GH definition, the document-context split above —
  all had to be learned from documentation and trial-and-error. Roughly 3–4 days were spent on
  pure familiarization before writing any plugin-specific logic, treated as a dedicated phase
  rather than folded into build time.
- **Rhino trial license expiration.** Rhino's free trial — the only license available for this
  project — expires roughly 90 days after install, which blocks all plugin-side testing until
  renewed. This recurred at least twice: once mid-project, and again by the time of the most
  recent engineering review, where backend and browser-side fixes could be verified but not
  re-confirmed inside Rhino/Grasshopper itself because the trial had lapsed again. Worked around
  each time by registering a fresh trial under a different email rather than purchasing a
  license. If the plugin stays core to the product, this is a recurring cost worth budgeting for
  deliberately rather than continuing to work around.

## Known limitation inherited from the backend

The buildable envelope the plugin extrudes into a 3D mass is currently a **ground-floor
footprint only** — it does not step in for upper floors the way most DCPs actually require
(separate upper-storey side setbacks and height-plane angle controls are cited by the API but
not yet applied to the returned geometry). This isn't a plugin bug — the geometry comes from the
backend as-is — but it directly affects what gets modelled in Rhino, so it's worth knowing:
the mass output (`c`) should currently be read as a footprint-and-height envelope, not a fully
accurate stepped massing constraint.

## Dependencies

- Rhino + Grasshopper (the Python Script component specifically) — no separate Python
  environment or `pip install` needed; it runs inside Rhino's own IronPython/CPython engine.
- The backend running and reachable at `http://localhost:8000`.
