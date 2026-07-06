# =============================================================================
# GRASSHOPPER PLUGIN — REFERENCE COPY ONLY
# This code runs inside RhinoPlugInV1.gh (Grasshopper Python Script component).
# It cannot be executed from the terminal — Rhino.Geometry only exists inside
# Rhino's scripting environment. Open RhinoPlugInV1.gh in Rhino to run this.
#
# It calls the FastAPI backend at localhost:8000, converts GeoJSON responses into
# Rhino geometry, and prints key buildable volume numbers to the GH console.
# Inputs:  x = address (string), y = run (bool)
# Outputs: a = lot curve, b = envelope curve, c = 3D mass
#          d = rules text,  e = lmr text,     f = controls text
# =============================================================================

"""
import Rhino.Geometry as rg
import math
import json

try:
    from urllib.request import urlopen, Request
except ImportError:
    from urllib2 import urlopen, Request

a = b = c = None
d = e = f = ""

BACKEND = "http://localhost:8000"
address = x if x else ""
run     = y if y else False


# Sends a POST request to the FastAPI backend with a JSON payload.
# Returns the parsed JSON response as a Python dictionary.
def post_json(endpoint, payload):
    body = json.dumps(payload).encode("utf-8")
    req  = Request(BACKEND + endpoint, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urlopen(req, timeout=30).read().decode("utf-8"))


# Converts a GeoJSON polygon into a Rhino NurbsCurve by translating lat/lon to metres.
# The lot centroid (o_lng, o_lat) is used as the local origin so geometry sits near (0,0) in Rhino.
def to_curve(geo, o_lng, o_lat):
    cos_lat = math.cos(math.radians(o_lat))
    pts = [rg.Point3d((p[0]-o_lng)*111320*cos_lat, (p[1]-o_lat)*110540, 0)
           for p in geo["coordinates"][0]]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return rg.Polyline(pts).ToNurbsCurve()


# The backend can return the envelope as either a GeoJSON dict or a list (serialised tuple).
# This normalises both cases to a plain dict so the rest of the code handles one format only.
def normalise_envelope(raw):
    return raw[0] if isinstance(raw, list) else raw


# Searches the rules list returned by the backend for a specific parameter (e.g. "max_height").
# Returns the value of the first match, or None if the parameter is not present.
def rule_val(rules, param):
    return next((r["value"] for r in rules if r.get("parameter") == param), None)


# Calculates the area of a GeoJSON polygon ring in square metres using the shoelace formula.
# Converts degrees to metres with a flat-earth approximation (accurate enough for lot-scale geometry).
def poly_area(ring):
    lat0 = ring[0][1]
    mLat = 111000.0
    mLon = 111000.0 * math.cos(math.radians(lat0))
    area = 0.0
    for i in range(len(ring)-1):
        x1, y1 = ring[i][0]*mLon,   ring[i][1]*mLat
        x2, y2 = ring[i+1][0]*mLon, ring[i+1][1]*mLat
        area  += x1*y2 - x2*y1
    return abs(area/2)


if not run:
    d = "Set run = True to execute"
elif not address:
    d = "Enter an address"
else:
    try:
        # Call backend — all geocoding, rule matching, envelope geometry done server-side
        data = post_json("/envelope", {"address": address})
        lmr  = post_json("/lmr",      {"address": address})

        lot_geo = data["lot_polygon"]
        env_geo = normalise_envelope(data["envelope"])
        rules   = data.get("rules_applied", [])

        # Lot centroid used as local coordinate origin so Rhino geometry sits near (0,0)
        ring  = lot_geo["coordinates"][0]
        o_lng = sum(p[0] for p in ring) / len(ring)
        o_lat = sum(p[1] for p in ring) / len(ring)

        # OUTPUT a: lot boundary, OUTPUT b: setback envelope
        a = to_curve(lot_geo, o_lng, o_lat)
        b = to_curve(env_geo, o_lng, o_lat) if env_geo and env_geo.get("coordinates") else None

        # Key control values from rules (height from LEP map, FSR from LEP map)
        lot_area = poly_area(ring)
        env_area = poly_area(env_geo["coordinates"][0]) if env_geo else 0
        height   = rule_val(rules, "max_height") or 8.5
        fsr      = rule_val(rules, "fsr")

        # OUTPUT c: extrude envelope curve vertically by max height to get 3D mass
        if b:
            breps = rg.Brep.CreatePlanarBreps(b)
            if breps:
                footprint = rg.AreaMassProperties.Compute(breps[0]).Area
                extruded  = rg.Surface.CreateExtrusion(b, rg.Vector3d(0, 0, height))
                if extruded:
                    c      = rg.Brep.CapPlanarHoles(extruded.ToBrep(), 0.01)
                    volume = rg.VolumeMassProperties.Compute(c).Volume if c else 0

                    print("Lot area          : {:.1f} sqm".format(lot_area))
                    print("Envelope footprint: {:.1f} sqm".format(footprint))
                    print("Max height        : {:.1f} m".format(height))
                    print("Buildable volume  : {:.1f} m3".format(volume))
                    if fsr:
                        print("Max floor area    : {:.1f} sqm (FSR {})".format(lot_area * float(fsr), fsr))

        # OUTPUT d: all applied rules as plain text for GH panel
        d = "\n".join("{}: {} {}".format(r["parameter"], r["value"], r.get("unit", ""))
                      for r in rules)

        # OUTPUT e: LMR eligibility summary
        e = "Zone: {}\nLMR eligible: {}\nStatus: {}".format(
            data.get("zone"), lmr.get("lmr_zone_eligible"), lmr.get("lmr_status", "N/A"))

        # OUTPUT f: key site metrics
        f = "Lot: {:.1f} sqm\nEnvelope: {:.1f} sqm\nHeight: {} m\nFSR: {}".format(
            lot_area, env_area, height, fsr or "N/A")

    except Exception:
        import traceback
        d = traceback.format_exc()
"""
