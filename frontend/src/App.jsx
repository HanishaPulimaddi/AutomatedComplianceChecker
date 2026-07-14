import { useState, useEffect, useRef } from "react";
import { MapContainer, TileLayer, Polygon, Popup, useMap } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import "./App.css";

/* ── helpers ── */

function sentenceCase(str) {
  if (!str) return str;
  return str.charAt(0).toUpperCase() + str.slice(1);
}

// Known acronyms that should stay fully upper-case rather than being
// title-cased word-by-word (e.g. "adg_balcony_min_area" should read
// "ADG Balcony Min Area", not "Adg Balcony Min Area").
const PARAM_LABEL_ACRONYMS = new Set([
  "adg", "cdc", "cdc3b", "tod", "fsr", "gfa", "sepp", "dcp", "lep", "pos",
]);

// Fallback formatter for any parameter without a manually curated entry in
// PARAM_LABELS below — guarantees every control name is displayed the same
// way (Title Case, spaces, no underscores) instead of the raw internal
// name, which is inconsistent by construction (parameter names get added
// over time by whoever built that extraction, not to a single style guide).
function formatParamLabel(param) {
  return param
    .split("_")
    .map((word) => (PARAM_LABEL_ACRONYMS.has(word) ? word.toUpperCase() : sentenceCase(word)))
    .join(" ");
}

function polygonAreaSqm(geoJsonCoords) {
  const ring = geoJsonCoords[0]; // [[lng, lat], ...]
  if (!ring || ring.length < 3) return 0;
  const lat0 = ring[0][1];
  const mLat = 111000;
  const mLon = 111000 * Math.cos(lat0 * Math.PI / 180);
  let area = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    const x1 = ring[i][0] * mLon,   y1 = ring[i][1] * mLat;
    const x2 = ring[i+1][0] * mLon, y2 = ring[i+1][1] * mLat;
    area += x1 * y2 - x2 * y1;
  }
  return Math.round(Math.abs(area / 2));
}

/* ── constants ── */

const PARAM_LABELS = {
  front_setback:                    "Front Setback",
  rear_setback:                     "Rear Setback",
  rear_setback_upper:               "Rear Setback (Upper Floor)",
  side_setback_ground:              "Side Setback (Ground Floor)",
  side_setback_upper:               "Side Setback (Upper Floor)",
  max_height:                       "Maximum Height",
  max_wall_height:                  "Maximum Wall Height",
  max_storeys:                      "Maximum Storeys",
  height_plane:                     "Height Plane Angle",
  fsr:                              "Floor Space Ratio",
  site_coverage_pct:                "Site Coverage",
  landscaped_area_pct:              "Landscaped Area",
  private_open_space:               "Private Open Space",
  private_open_space_min_dimension: "Open Space Min. Dimension",
  building_separation:              "Building Separation",
  front_fence_height_solid:         "Front Fence — Solid",
  front_fence_height_open:          "Front Fence — Open",
  side_fence_height:                "Side Fence Height",
  rear_fence_height:                "Rear Fence Height",
  parking_spaces_per_dwelling:      "Car Spaces per Dwelling",
  max_driveway_width:               "Max Driveway Width",
};

const GROUPS = [
  {
    key: "setbacks",
    label: "Setbacks",
    color: "#6366f1",
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M21 3H3v18h18V3z" /><path d="M9 3v18M3 9h6M3 15h6" />
      </svg>
    ),
    match: (p) => p.includes("setback"),
  },
  {
    key: "height",
    label: "Height Controls",
    color: "#d97706",
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M3 20h18M7 20V10l5-7 5 7v10M9 20v-5h6v5" />
      </svg>
    ),
    match: (p) => ["max_height", "max_storeys", "height_plane"].includes(p),
  },
  {
    key: "landscaping",
    label: "Landscaping & Open Space",
    color: "#16a34a",
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 22V12M12 12C12 7 7 3 3 3c0 4 3 9 9 9zM12 12c0-5 5-9 9-9-0 4-3 9-9 9z" />
      </svg>
    ),
    match: (p) => p.includes("landscaped") || p.includes("open_space"),
  },
];

const EXAMPLE_ADDRESSES = [
  "35 Connecticut Avenue Five Dock 2046",
  "110 Correys Avenue Concord 2137",
  "10 Wrights Road Drummoyne 2047",
  "15 Gauthorpe Street Rhodes NSW 2138",
];

// In local dev, this is empty — every fetch() call below is a relative path
// like "/envelope", and vite.config.js's proxy forwards it to the backend
// on localhost:8000, which is why you see "localhost" in the browser today.
// That proxy only exists in the dev server; it does NOT exist once this is
// built and deployed. If the frontend and backend end up on two different
// domains in production (e.g. frontend on Vercel, backend on a separate
// host — likely, since this backend's dependencies, shapely/numpy/PyMuPDF,
// don't fit Vercel's serverless model well), every relative fetch AND every
// PDF link would silently try to hit the FRONTEND's own domain instead of
// the backend, and 404. Setting VITE_API_BASE_URL at build time (e.g. to
// "https://your-backend.onrender.com") fixes this for both.
const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

/* ── map helper ── */

function MapUpdater({ bounds }) {
  const map = useMap();
  useEffect(() => {
    if (bounds) map.fitBounds(bounds, { padding: [56, 56] });
  }, [bounds, map]);
  return null;
}

/* ── main app ── */

export default function App() {
  const [address, setAddress]       = useState("");
  const [result, setResult]         = useState(null);
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState(null);
  const [activeRule, setActiveRule] = useState(null);
  const [mapMode, setMapMode]       = useState("satellite"); // "satellite" | "plan"
  const [suggestions, setSuggestions]     = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [highlightIndex, setHighlightIndex]   = useState(-1);
  const inputRef = useRef(null);
  const suggestSeq = useRef(0); // guards against an out-of-order late response overwriting a newer one

  // Autocomplete-as-you-type: most people don't type a full, correctly
  // formatted address, so this asks the backend (which forwards to the same
  // geocoder /envelope uses) for ranked suggestions on every keystroke,
  // debounced so it's not one network call per character.
  useEffect(() => {
    if (!address.trim() || address.trim().length < 3) {
      setSuggestions([]);
      return;
    }
    const seq = ++suggestSeq.current;
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`${API_BASE}/address-suggestions?q=${encodeURIComponent(address)}`);
        if (!res.ok) return;
        const data = await res.json();
        if (seq === suggestSeq.current) {
          setSuggestions(data.suggestions ?? []);
          setHighlightIndex(-1);
        }
      } catch {
        // Suggestions are a convenience — a failed lookup just means no dropdown, not an error.
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [address]);

  async function handleSubmit(e, overrideAddress) {
    e?.preventDefault();
    const toSearch = overrideAddress ?? address;
    if (!toSearch.trim()) return;
    setShowSuggestions(false);
    setLoading(true);
    setError(null);
    setResult(null);
    setActiveRule(null);
    try {
      const res = await fetch(`${API_BASE}/envelope`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: toSearch.trim() }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error (${res.status})`);
      }
      setResult(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function selectSuggestion(text) {
    setAddress(text);
    setSuggestions([]);
    setShowSuggestions(false);
    handleSubmit(null, text);
  }

  function handleInputKeyDown(e) {
    if (!showSuggestions || suggestions.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIndex((i) => (i + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIndex((i) => (i - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === "Enter" && highlightIndex >= 0) {
      e.preventDefault();
      selectSuggestion(suggestions[highlightIndex]);
    } else if (e.key === "Escape") {
      setShowSuggestions(false);
    }
  }

  function handleExample() {
    const ex = EXAMPLE_ADDRESSES[Math.floor(Math.random() * EXAMPLE_ADDRESSES.length)];
    setAddress(ex);
    setShowSuggestions(false);
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  const toLeaflet = (coords) => coords.map(([lng, lat]) => [lat, lng]);

  // Normalise envelope — backend may return a list (tuple serialised) or a GeoJSON dict
  const envelopeGeoJson = result
    ? (Array.isArray(result.envelope) ? result.envelope[0] : result.envelope)
    : null;

  const lotCoords = result?.lot_polygon?.coordinates?.[0]
    ? toLeaflet(result.lot_polygon.coordinates[0]) : null;

  const envelopeCoords = envelopeGeoJson?.coordinates?.[0]
    ? toLeaflet(envelopeGeoJson.coordinates[0]) : null;

  const mapBounds = lotCoords
    ? [
        [Math.min(...lotCoords.map((p) => p[0])), Math.min(...lotCoords.map((p) => p[1]))],
        [Math.max(...lotCoords.map((p) => p[0])), Math.max(...lotCoords.map((p) => p[1]))],
      ]
    : null;

  // Derived metrics computed client-side from polygon geometry + rules
  const metrics = result ? (() => {
    const lotArea      = result.lot_polygon?.coordinates ? polygonAreaSqm(result.lot_polygon.coordinates) : null;
    const envArea      = envelopeGeoJson?.coordinates    ? polygonAreaSqm(envelopeGeoJson.coordinates)    : null;
    const coverage     = lotArea && envArea ? Math.round((envArea / lotArea) * 100) : null;
    const fsrRule      = result.rules_applied?.find(r => r.parameter === "fsr");
    const fsr          = fsrRule?.value ?? null;
    const maxFloorArea = lotArea && fsr ? Math.round(lotArea * fsr) : null;
    return { lotArea, envArea, coverage, fsr, maxFloorArea };
  })() : null;

  return (
    <div className="root">
      {/* ── Header ── */}
      <header className="header">
        <div className="brand">
          <div className="brand-icon-wrap" aria-hidden="true">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2">
              <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/><path d="M7 8h2v6H7zM11 10h2v4h-2zM15 6h2v8h-2z"/>
            </svg>
          </div>
          <span className="brand-name">PlanCheck</span>
          <div className="brand-divider" aria-hidden="true" />
          <span className="brand-tag">Canada Bay · R1–R4</span>
        </div>

        <form onSubmit={handleSubmit} className="search-form" role="search" aria-label="Address search">
          <div className="search-wrap" style={{ position: "relative" }}>
            <svg className="search-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>
            </svg>
            <input
              ref={inputRef}
              value={address}
              onChange={(e) => { setAddress(e.target.value); setShowSuggestions(true); }}
              onFocus={() => setShowSuggestions(true)}
              onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
              onKeyDown={handleInputKeyDown}
              placeholder="Enter a Canada Bay address — e.g. 35 Connecticut Ave Five Dock"
              className="search-input"
              disabled={loading}
              autoComplete="off"
              aria-label="Canada Bay address"
              aria-autocomplete="list"
              aria-expanded={showSuggestions && suggestions.length > 0}
            />
            {address && (
              <button
                type="button"
                className="clear-btn"
                onClick={() => { setAddress(""); setSuggestions([]); inputRef.current?.focus(); }}
                aria-label="Clear address"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden="true">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            )}
            {showSuggestions && suggestions.length > 0 && (
              <ul className="address-suggestions" role="listbox">
                {suggestions.map((s, i) => (
                  <li
                    key={s}
                    role="option"
                    aria-selected={i === highlightIndex}
                    className={`address-suggestion-item${i === highlightIndex ? " address-suggestion-item--active" : ""}`}
                    onMouseDown={() => selectSuggestion(s)}
                    onMouseEnter={() => setHighlightIndex(i)}
                  >
                    {s}
                  </li>
                ))}
              </ul>
            )}
            {showSuggestions && suggestions.length === 0 && address.trim().length >= 3 && !/^\d/.test(address.trim()) && (
              <ul className="address-suggestions" role="listbox">
                <li className="address-suggestion-item address-suggestion-item--hint">
                  No address match yet — try adding the house number, e.g. "5 {address.trim()}"
                </li>
              </ul>
            )}
          </div>
          <button type="submit" disabled={loading || !address.trim()} className="submit-btn">
            {loading
              ? <><span className="spinner" aria-hidden="true" /> Checking…</>
              : <>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  Check Site
                </>
            }
          </button>
        </form>
      </header>

      {/* ── Body ── */}
      <div className="body">
        {/* Map / Diagram */}
        <div className="map-wrap" role="region" aria-label="Property map">

          {/* Toggle tabs */}
          <div className="view-toggle">
            <button className={`view-tab${mapMode === "satellite" ? " view-tab--active" : ""}`} onClick={() => setMapMode("satellite")}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>
              Satellite
            </button>
            <button className={`view-tab${mapMode === "plan" ? " view-tab--active" : ""}`} onClick={() => setMapMode("plan")} disabled={!result}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="1"/><path d="M3 9h18M9 21V9"/></svg>
              Site Plan
            </button>
          </div>

          {/* Satellite map */}
          <div style={{ position: "absolute", inset: 0, visibility: mapMode === "satellite" ? "visible" : "hidden" }}>
            <MapContainer
              center={[-33.86, 151.1]}
              zoom={14}
              style={{ height: "100%", width: "100%" }}
              zoomControl={true}
            >
              <TileLayer
                url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
                attribution='Tiles &copy; Esri'
                maxZoom={19}
              />
              <TileLayer
                url="https://maps.six.nsw.gov.au/arcgis/rest/services/sixmaps/LPI_Cadastre_Imagery/MapServer/tile/{z}/{y}/{x}"
                attribution='&copy; <a href="https://www.spatial.nsw.gov.au" target="_blank" rel="noreferrer">NSW Spatial Services</a>'
                opacity={0.6}
                maxZoom={19}
              />
              {lotCoords && (
                <Polygon positions={lotCoords} pathOptions={{ color: "#facc15", fillColor: "#fde68a", fillOpacity: 0.18, weight: 2.5 }} />
              )}
              {envelopeCoords && (
                <Polygon positions={envelopeCoords} pathOptions={{ color: "#f97316", fillColor: "#fb923c", fillOpacity: 0.35, weight: 2, dashArray: "6 4" }}>
                  {metrics && (
                    <Popup className="envelope-popup">
                      <div className="ep-title">Buildable Envelope</div>
                      <div className="ep-grid">
                        {metrics.envArea      && <><span>Footprint</span><strong>{metrics.envArea.toLocaleString()} m²</strong></>}
                        {metrics.coverage     && <><span>Coverage</span><strong>{metrics.coverage}%</strong></>}
                        {metrics.lotArea      && <><span>Lot area</span><strong>{metrics.lotArea.toLocaleString()} m²</strong></>}
                        {metrics.fsr          && <><span>FSR</span><strong>{metrics.fsr}:1</strong></>}
                        {metrics.maxFloorArea && <><span>Max GFA</span><strong>{metrics.maxFloorArea.toLocaleString()} m²</strong></>}
                      </div>
                      <div className="ep-note">Click Site Plan tab for dimensions</div>
                    </Popup>
                  )}
                </Polygon>
              )}
              {mapBounds && <MapUpdater bounds={mapBounds} />}
            </MapContainer>
          </div>

          {/* Site plan diagram */}
          {mapMode === "plan" && result && (
            <SitePlanDiagram result={result} metrics={metrics} />
          )}
        </div>

        {/* Panel */}
        <aside className="panel" aria-label="Compliance results">
          {!result && !loading && !error && <EmptyState onExample={handleExample} />}
          {loading && <LoadingState />}
          {error && <ErrorState message={error} onDismiss={() => setError(null)} />}
          {result && (
            <ResultPanel result={result} metrics={metrics} activeRule={activeRule} onRuleClick={setActiveRule} />
          )}
        </aside>
      </div>

      {/* ── Grasshopper footer ── */}
      <footer className="gh-footer">
        <div className="gh-footer-inner">
          <div className="gh-footer-left">
            <div className="gh-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                <path d="M2 17l10 5 10-5"/>
                <path d="M2 12l10 5 10-5"/>
              </svg>
            </div>
            <div>
              <span className="gh-footer-title">Grasshopper Plugin</span>
              <span className="gh-footer-sub">Model the buildable envelope directly in Rhino</span>
            </div>
          </div>
          <div className="gh-footer-actions">
            {/* A browser can't detect whether Rhino/Grasshopper is already
                installed on someone's machine (no web API exposes that), so
                both options are shown — the visitor picks based on what they
                already have. */}
            <a
              href="https://www.rhino3d.com/download/"
              target="_blank"
              rel="noreferrer"
              className="gh-get-rhino-link"
            >
              Don't have Rhino? Get it here (Grasshopper's included) ↗
            </a>
            <a href={`${API_BASE}/grasshopper-plugin`} className="gh-download-btn" download>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
              Download Plugin
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}

/* ── site plan diagram ── */

function SitePlanDiagram({ result, metrics }) {
  const W = 640, H = 520, PAD = 72;

  const lotRing = result.lot_polygon.coordinates[0];
  const envelopeGeom = Array.isArray(result.envelope) ? result.envelope[0] : result.envelope;
  const envRing = envelopeGeom.coordinates[0];

  // Centroid from lot ring (exclude closing point)
  const n = lotRing.length - 1;
  const clat = lotRing.slice(0, n).reduce((s, c) => s + c[1], 0) / n;
  const clon = lotRing.slice(0, n).reduce((s, c) => s + c[0], 0) / n;
  const mLat = 111000;
  const mLon = 111000 * Math.cos(clat * Math.PI / 180);

  // GeoJSON → metres, Y-flipped for SVG (north = up)
  const toM  = ([lng, lat]) => [(lng - clon) * mLon, (clat - lat) * mLat];
  const lotM = lotRing.map(toM);
  const envM = envRing.map(toM);

  // Bounding box
  const xs = lotM.map(p => p[0]), ys = lotM.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const wM = maxX - minX, hM = maxY - minY;
  const scale = Math.min((W - 2 * PAD) / wM, (H - 2 * PAD) / hM);
  const ox = (W - wM * scale) / 2 - minX * scale;
  const oy = (H - hM * scale) / 2 - minY * scale;
  const toSVG = ([mx, my]) => [+(mx * scale + ox).toFixed(1), +(my * scale + oy).toFixed(1)];

  const lotSVG = lotM.map(toSVG);
  const envSVG = envM.map(toSVG);
  const toPath = pts => pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ") + " Z";

  // Front edge: nearest lot edge midpoint to geocoded road point (fallback to centroid offset)
  const geoM_pt = (result.lon && result.lat) ? toM([result.lon, result.lat]) : [0, maxY + 10];
  let frontIdx = 0, minD = Infinity;
  for (let i = 0; i < lotM.length - 1; i++) {
    const mx = (lotM[i][0] + lotM[i+1][0]) / 2;
    const my = (lotM[i][1] + lotM[i+1][1]) / 2;
    const d  = (mx - geoM_pt[0]) ** 2 + (my - geoM_pt[1]) ** 2;
    if (d < minD) { minD = d; frontIdx = i; }
  }

  // Rear edge: lot edge midpoint furthest from front midpoint
  const fmx = (lotM[frontIdx][0] + lotM[frontIdx+1][0]) / 2;
  const fmy = (lotM[frontIdx][1] + lotM[frontIdx+1][1]) / 2;
  let rearIdx = 0, maxD = -1;
  for (let i = 0; i < lotM.length - 1; i++) {
    if (i === frontIdx) continue;
    const mx = (lotM[i][0] + lotM[i+1][0]) / 2;
    const my = (lotM[i][1] + lotM[i+1][1]) / 2;
    const d  = (mx - fmx) ** 2 + (my - fmy) ** 2;
    if (d > maxD) { maxD = d; rearIdx = i; }
  }

  // Setback values from rules
  const rules = result.rules_applied ?? [];
  const getVal = p => rules.find(r => r.parameter === p)?.value;
  const fsb = getVal("front_setback") ?? 4.5;
  const rsb = getVal("rear_setback")  ?? 6.0;
  const ssb = getVal("side_setback_ground") ?? 0.9;

  // Inward normal for an edge (toward centroid)
  function inwardNormal(edgeIdx) {
    const p1 = lotSVG[edgeIdx], p2 = lotSVG[edgeIdx+1];
    const dx = p2[0]-p1[0], dy = p2[1]-p1[1];
    const len = Math.sqrt(dx*dx+dy*dy)||1;
    let nx = -dy/len, ny = dx/len;
    const mid = [(p1[0]+p2[0])/2, (p1[1]+p2[1])/2];
    if (nx*(W/2-mid[0]) + ny*(H/2-mid[1]) < 0) { nx=-nx; ny=-ny; }
    return [nx, ny];
  }

  // Setback dimension: arrow inside lot from edge to envelope, or external pill if too small
  function setbackArrow(edgeIdx, sbM, color) {
    const label = `${sbM}m`;
    const p1 = lotSVG[edgeIdx], p2 = lotSVG[edgeIdx+1];
    const mid = [(p1[0]+p2[0])/2, (p1[1]+p2[1])/2];
    const [nx, ny] = inwardNormal(edgeIdx);
    const sbPx = sbM * scale;
    const tip  = [mid[0] + nx*sbPx, mid[1] + ny*sbPx];
    const mid2 = [(mid[0]+tip[0])/2, (mid[1]+tip[1])/2];
    const cid  = color.replace('#','');

    if (sbPx < 20) {
      // Too small for an arrow inside — place pill outside the lot edge
      const ox = -nx * 26, oy = -ny * 26;
      return (
        <g key={"sb"+edgeIdx}>
          <line x1={mid[0]} y1={mid[1]} x2={mid[0]+ox} y2={mid[1]+oy}
            stroke={color} strokeWidth="1" strokeDasharray="3 2" />
          <rect x={mid[0]+ox-18} y={mid[1]+oy-9} width="36" height="18" rx="4"
            fill="white" stroke={color} strokeWidth="1.2" />
          <text x={mid[0]+ox} y={mid[1]+oy+4.5} textAnchor="middle"
            fontSize="11" fontWeight="700" fill={color}>{label}</text>
        </g>
      );
    }
    return (
      <g key={"sb"+edgeIdx}>
        <defs>
          <marker id={`a-${cid}`} markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto">
            <path d="M0,0 L6,3 L0,6 Z" fill={color}/>
          </marker>
        </defs>
        <line x1={mid[0]+nx*4} y1={mid[1]+ny*4} x2={tip[0]} y2={tip[1]}
          stroke={color} strokeWidth="1.5" markerEnd={`url(#a-${cid})`}/>
        <rect x={mid2[0]-18} y={mid2[1]-9} width="36" height="18" rx="4"
          fill="white" stroke={color} strokeWidth="1.2"/>
        <text x={mid2[0]} y={mid2[1]+4.5} textAnchor="middle"
          fontSize="11" fontWeight="700" fill={color}>{label}</text>
      </g>
    );
  }

  // Lot edge length in metres
  function edgeLenM(i) {
    const dx = lotM[i+1][0]-lotM[i][0], dy = lotM[i+1][1]-lotM[i][1];
    return Math.round(Math.sqrt(dx*dx+dy*dy));
  }

  const sideEdges = Array.from({length: lotM.length-1}, (_,i) => i)
    .filter(i => i !== frontIdx && i !== rearIdx);

  // Dimension label placed outward from edge midpoint at given px offset, with role subtitle
  function dimLabel(edgeIdx, role, outPx) {
    const p1 = lotSVG[edgeIdx], p2 = lotSVG[edgeIdx+1];
    const mid = [(p1[0]+p2[0])/2, (p1[1]+p2[1])/2];
    const [nx, ny] = inwardNormal(edgeIdx);
    const lx = mid[0] - nx * outPx, ly = mid[1] - ny * outPx;
    // angle of edge for rotated text
    const dx = p2[0]-p1[0], dy = p2[1]-p1[1];
    const angleDeg = Math.atan2(dy, dx) * 180 / Math.PI;
    // keep text readable — flip if upside down
    const rot = (angleDeg > 90 || angleDeg < -90) ? angleDeg + 180 : angleDeg;
    return (
      <g key={"dl"+edgeIdx} transform={`rotate(${rot},${lx},${ly})`}>
        <text x={lx} y={ly - 3} textAnchor="middle" fontSize="11" fontWeight="700" fill="#475569">
          {edgeLenM(edgeIdx)}m
        </text>
        <text x={lx} y={ly + 9} textAnchor="middle" fontSize="8" fill="#94a3b8" letterSpacing="0.8">
          {role}
        </text>
      </g>
    );
  }

  // Scale bar: 10m
  const scalePx10 = scale * 10;

  return (
    <div className="site-plan-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="site-plan-svg" aria-label="Site plan diagram">
        <defs>
          {[["#6366f1","arr-6366f1"],["#f97316","arr-f97316"],["#16a34a","arr-16a34a"]].map(([col, id]) => (
            <marker key={id} id={id} markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill={col} />
            </marker>
          ))}
          <pattern id="hatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="8" stroke="#bfdbfe" strokeWidth="3" />
          </pattern>
        </defs>

        {/* Background */}
        <rect width={W} height={H} fill="#f1f5f9" />

        {/* Lot fill */}
        <path d={toPath(lotSVG)} fill="#f8fafc" stroke="#334155" strokeWidth="2.5" />

        {/* Setback zone (space between lot and envelope) hatched */}
        <path d={`${toPath(lotSVG)} ${toPath([...envSVG].reverse())}`}
          fill="url(#hatch)" fillRule="evenodd" opacity="0.7" />

        {/* Envelope fill */}
        <path d={toPath(envSVG)} fill="#dbeafe" stroke="#3b82f6" strokeWidth="2" strokeDasharray="7 3" />

        {/* Street edge highlight */}
        <line
          x1={lotSVG[frontIdx][0]} y1={lotSVG[frontIdx][1]}
          x2={lotSVG[frontIdx+1][0]} y2={lotSVG[frontIdx+1][1]}
          stroke="#ef4444" strokeWidth="5" strokeLinecap="round"
        />
        {/* STREET label — 22px outward from front edge, parallel to edge */}
        {(() => {
          const p1 = lotSVG[frontIdx], p2 = lotSVG[frontIdx+1];
          const mid = [(p1[0]+p2[0])/2, (p1[1]+p2[1])/2];
          const [nx, ny] = inwardNormal(frontIdx);
          const lx = mid[0]-nx*22, ly = mid[1]-ny*22;
          const dx = p2[0]-p1[0], dy = p2[1]-p1[1];
          const angleDeg = Math.atan2(dy, dx) * 180 / Math.PI;
          const rot = (angleDeg > 90 || angleDeg < -90) ? angleDeg + 180 : angleDeg;
          return <text x={lx} y={ly+4} textAnchor="middle" fontSize="10" fontWeight="700"
            fill="#ef4444" letterSpacing="1" transform={`rotate(${rot},${lx},${ly})`}>STREET</text>;
        })()}

        {/* Setback arrows — front and rear inside lot; skip side pill (too small, hatch shows zone) */}
        {setbackArrow(frontIdx, fsb, "#6366f1")}
        {setbackArrow(rearIdx,  rsb, "#6366f1")}

        {/* Dimension labels — parallel to each edge, outward offset so they never overlap pills */}
        {dimLabel(frontIdx, "FRONT", 52)}
        {dimLabel(rearIdx,  "REAR",  48)}
        {sideEdges.slice(0, 1).map(i => dimLabel(i, "SIDE", 44))}
        {sideEdges.length > 1 && dimLabel(sideEdges[sideEdges.length-1], "SIDE", 44)}

        {/* North compass — top right, proper two-tone needle */}
        <g transform={`translate(${W-48}, 48)`}>
          <circle cx="0" cy="0" r="22" fill="white" stroke="#e2e8f0" strokeWidth="1.5"
            style={{filter:"drop-shadow(0 1px 3px rgba(0,0,0,0.1))"}}/>
          {/* North needle — dark */}
          <path d="M0,-15 L4.5,0 L0,-3 Z" fill="#1e293b"/>
          {/* South needle — light */}
          <path d="M0,-3 L-4.5,0 L0,15 L4.5,0 Z" fill="#cbd5e1"/>
          {/* Centre dot */}
          <circle cx="0" cy="0" r="2" fill="#1e293b"/>
          {/* Tick marks */}
          <line x1="0" y1="-19" x2="0" y2="-22" stroke="#94a3b8" strokeWidth="1.5"/>
          <line x1="19" y1="0"  x2="22" y2="0"  stroke="#94a3b8" strokeWidth="1.5"/>
          <line x1="0" y1="19" x2="0"  y2="22"  stroke="#94a3b8" strokeWidth="1.5"/>
          <line x1="-19" y1="0" x2="-22" y2="0" stroke="#94a3b8" strokeWidth="1.5"/>
          <text x="0" y="-26" textAnchor="middle" fontSize="9" fontWeight="800"
            fill="#1e293b" letterSpacing="0.5">N</text>
        </g>

        {/* Scale bar — bottom right, inside SVG bounds */}
        <g transform={`translate(${W - 16 - scalePx10}, ${H - 30})`}>
          <rect x="0" y="0" width={scalePx10/2} height="6" fill="#334155"/>
          <rect x={scalePx10/2} y="0" width={scalePx10/2} height="6" fill="white" stroke="#334155" strokeWidth="1"/>
          <rect x="0" y="0" width={scalePx10} height="6" fill="none" stroke="#334155" strokeWidth="1"/>
          <text x="0" y="18" fontSize="9" fill="#64748b" textAnchor="middle">0</text>
          <text x={scalePx10/2} y="18" fontSize="9" fill="#64748b" textAnchor="middle">5m</text>
          <text x={scalePx10} y="18" fontSize="9" fill="#64748b" textAnchor="middle">10m</text>
        </g>

        {/* Legend — bottom left */}
        <g transform={`translate(16, ${H-70})`}>
          <rect x="0" y="0" width="130" height="66" rx="6" fill="white" stroke="#e2e8f0" strokeWidth="1" />
          <rect x="8" y="10" width="14" height="10" fill="#dbeafe" stroke="#3b82f6" strokeWidth="1.5" strokeDasharray="4 2" />
          <text x="28" y="20" fontSize="10" fill="#334155">Buildable envelope</text>
          <rect x="8" y="28" width="14" height="10" fill="url(#hatch)" stroke="#94a3b8" strokeWidth="1" />
          <text x="28" y="38" fontSize="10" fill="#334155">Setback zone</text>
          <line x1="8" y1="52" x2="22" y2="52" stroke="#ef4444" strokeWidth="3" />
          <text x="28" y="56" fontSize="10" fill="#334155">Street frontage</text>
        </g>

        {/* Metrics summary — top left */}
        {metrics && (
          <g transform="translate(16, 16)">
            <rect x="0" y="0" width="155" height={metrics.maxFloorArea ? 74 : 58} rx="6" fill="white" stroke="#e2e8f0" strokeWidth="1" />
            <text x="10" y="16" fontSize="9" fontWeight="700" fill="#94a3b8" letterSpacing="0.5">SITE METRICS</text>
            {metrics.lotArea  && <><text x="10" y="31" fontSize="10" fill="#64748b">Lot area</text><text x="145" y="31" textAnchor="end" fontSize="10" fontWeight="700" fill="#334155">{metrics.lotArea.toLocaleString()} m²</text></>}
            {metrics.envArea  && <><text x="10" y="46" fontSize="10" fill="#64748b">Buildable footprint</text><text x="145" y="46" textAnchor="end" fontSize="10" fontWeight="700" fill="#3b82f6">{metrics.envArea.toLocaleString()} m²</text></>}
            {metrics.maxFloorArea && <><text x="10" y="61" fontSize="10" fill="#64748b">Max GFA (FSR {metrics.fsr}:1)</text><text x="145" y="61" textAnchor="end" fontSize="10" fontWeight="700" fill="#6366f1">{metrics.maxFloorArea.toLocaleString()} m²</text></>}
          </g>
        )}
      </svg>
    </div>
  );
}

/* ── panel states ── */

function EmptyState({ onExample }) {
  return (
    <div className="empty-state">
      <div className="empty-icon" aria-hidden="true">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.4">
          <rect x="2" y="3" width="20" height="14" rx="2"/>
          <path d="M8 21h8M12 17v4"/>
          <path d="M7 8h2v6H7zM11 10h2v4h-2zM15 6h2v8h-2z"/>
        </svg>
      </div>
      <h2 className="empty-title">Check any residential address</h2>
      <p className="empty-body">
        Enter a residential address in <strong>Canada Bay</strong> to
        instantly retrieve all DCP development controls and compute the buildable envelope.
      </p>
      <button className="example-btn" onClick={onExample}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <polygon points="5 3 19 12 5 21 5 3" />
        </svg>
        Try an example address
      </button>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <div className="loading-spinner-wrap" aria-hidden="true">
        <div className="loading-spinner-ring" />
      </div>
      <p className="loading-title">Computing envelope…</p>
      <p className="loading-sub">Fetching lot &amp; zoning data from NSW APIs</p>
      <div className="loading-steps">
        {["Geocoding address", "Retrieving lot polygon", "Applying DCP rules"].map((step, i) => (
          <div key={i} className="loading-step" style={{ animationDelay: `${i * 0.45}s` }}>
            <span className="step-dot" aria-hidden="true" />
            {step}
          </div>
        ))}
      </div>
    </div>
  );
}

function ErrorState({ message, onDismiss }) {
  return (
    <div className="error-state" role="alert">
      <div className="error-icon" aria-hidden="true">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2">
          <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
        </svg>
      </div>
      <p className="error-title">Something went wrong</p>
      <p className="error-msg">{message}</p>
      <div className="error-actions">
        <button className="dismiss-btn" onClick={onDismiss}>Dismiss</button>
      </div>
    </div>
  );
}

/* ── result panel ── */

function ResultPanel({ result, metrics, activeRule, onRuleClick }) {
  const rules = result.rules_applied ?? [];
  const lga   = result.lga || result.council || null;

  const heightOrFsrRule = rules.find(
    (r) => (r.parameter === "fsr" || r.parameter === "max_height") &&
           (r.conditions ?? []).some((c) => c.toLowerCase().includes("exceptions to development standards") || c.toLowerCase().includes("exceeds this"))
  );

  return (
    <div className="result-panel">
      <div className="address-card">
        <div className="address-line">
          <svg className="address-pin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" aria-hidden="true">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
            <circle cx="12" cy="10" r="3" />
          </svg>
          <span className="address-text">{result.address}</span>
        </div>
        <div className="meta-row">
          <span className="zone-badge">Zone {result.zone}</span>
          {lga && <span className="lga-badge">{lga}</span>}
          {result.heritage && (
            <span className="heritage-badge" title={result.heritage.name || "Heritage listed"}>
              🏛 Heritage{result.heritage.category ? ` — ${result.heritage.category}` : ""}
            </span>
          )}
          <span className="rule-count">{rules.length} controls</span>
        </div>
        {metrics && (
          <div className="metrics-row">
            {metrics.lotArea    && <MetricPill label="Lot" value={`${metrics.lotArea.toLocaleString()} m²`} />}
            {metrics.envArea    && <MetricPill label="Footprint" value={`${metrics.envArea.toLocaleString()} m²`} color="#f97316" />}
            {metrics.coverage   && <MetricPill label="Coverage" value={`${metrics.coverage}%`} />}
            {metrics.maxFloorArea && <MetricPill label={`GFA (FSR ${metrics.fsr}:1)`} value={`${metrics.maxFloorArea.toLocaleString()} m²`} color="#6366f1" />}
          </div>
        )}
        {result.data_currency && (
          <div className="data-currency-note">
            {result.data_currency.live_map_data_age_days != null && (
              <span>Live map data: {result.data_currency.live_map_data_age_days}d old (refreshed every {result.data_currency.live_map_data_refreshed_every_days}d)</span>
            )}
            {Object.entries(result.data_currency.dcp_versions ?? {}).map(([doc, v]) => (
              <span key={doc} title={doc}>{doc.replace(/^Canada Bay /, "")}: v{v.version ?? "?"} ({v.date})</span>
            ))}
          </div>
        )}
      </div>

      {/* Made-up placeholder setback used instead of a real rule — this is
          an important warning to surface: it means part of the drawn
          shape is not backed by an actual council figure. */}
      {result.envelope_fallback_warnings?.some((w) => w.placeholder_value_m != null) && (
        <CaveatBanner type="danger" title="Some of this shape uses placeholder numbers, not real council rules">
          <ul className="caveat-list">
            {result.envelope_fallback_warnings.filter((w) => w.placeholder_value_m != null).map((w, i) => (
              <li key={i}>{w.message}</li>
            ))}
          </ul>
        </CaveatBanner>
      )}

      {/* Real DCP values, but possibly applied to the wrong physical edge —
          confirmed failure modes on lots wider than they are deep, and on
          lots with complex/curved (>12-vertex) boundaries. Surfaced
          separately from the placeholder-value warning above because the
          underlying numbers here ARE real; it's the shape they were
          applied to that's unverified. This is the single most important
          warning to notice: it's the one case where the shape can look
          completely normal while actually being wrong. */}
      {result.envelope_fallback_warnings?.some((w) => w.placeholder_value_m == null) && (
        <CaveatBanner type="danger" title="This lot's shape may have confused setback detection">
          <ul className="caveat-list">
            {result.envelope_fallback_warnings.filter((w) => w.placeholder_value_m == null).map((w, i) => (
              <li key={i}>{w.message}</li>
            ))}
          </ul>
        </CaveatBanner>
      )}

      {/* Five Dock Town Centre and similar carve-outs where no shape can be
          computed at all — the backend already explains why, this just
          makes sure that explanation actually reaches the page. */}
      {result.envelope_note && (
        <CaveatBanner type="warn" title="No shape drawn for this address">
          {result.envelope_note}
        </CaveatBanner>
      )}

      {/* Council can still approve more than the height/FSR number shown
          below, if justified — this was previously buried inside a long
          "Applies when" list on the individual rule card. */}
      {heightOrFsrRule && (
        <CaveatBanner type="info" title="This is a standard, not an absolute ceiling">
          Council can approve a building that exceeds the height or floor-space number shown below,
          if the applicant provides good enough justification. The figures below are the normal
          limit, not a hard cap that can never be crossed.
        </CaveatBanner>
      )}

      <CaveatBanner type="muted" title="Corner lots are not automatically detected">
        If this property has frontage to two streets, it may need an extra setback on the second
        street that isn't reflected below — please confirm this with council directly.
      </CaveatBanner>

      {/* The drawn shape is a flat ground-floor footprint — it doesn't narrow
          for the upper-storey setback or height plane, even though both are
          listed as separate rules below. Without this, the shape reads as a
          complete 3D massing guide when it isn't yet. */}
      {result.envelope_geometry_note && (
        <CaveatBanner type="muted" title="This shape is the ground-floor footprint only">
          {result.envelope_geometry_note}
        </CaveatBanner>
      )}

      <CdcEligibilitySection address={result.address} />

      {result.alternate_development_scenario && (
        <AlternateScenarioSection scenario={result.alternate_development_scenario} />
      )}

      {GROUPS.map((group) => {
        const list = rules.filter((r) => group.match(r.parameter));
        if (!list.length) return null;
        return (
          <RuleGroup key={group.key} group={group} rules={list} activeRule={activeRule} onRuleClick={onRuleClick} />
        );
      })}

      {(() => {
        const other = rules.filter((r) => !GROUPS.some((g) => g.match(r.parameter)));
        if (!other.length) return null;
        return (
          <RuleGroup
            group={{ key: "other", label: "Other Controls", color: "#94a3b8", icon: null }}
            rules={other}
            activeRule={activeRule}
            onRuleClick={onRuleClick}
          />
        );
      })()}
    </div>
  );
}

function CaveatBanner({ type, title, children }) {
  return (
    <div className={`caveat-banner caveat-banner--${type}`}>
      <span className="caveat-icon" aria-hidden="true">
        {type === "danger" ? "⛔" : type === "warn" ? "⚠️" : type === "info" ? "ℹ️" : "📍"}
      </span>
      <span>
        <span className="caveat-title">{title}</span>
        {children}
      </span>
    </div>
  );
}

function CdcEligibilitySection({ address }) {
  const [open, setOpen]       = useState(false);
  const [loading, setLoading] = useState(false);
  const [data, setData]       = useState(null);
  const [error, setError]     = useState(null);

  async function toggle() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (data || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/cdc-eligibility`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error (${res.status})`);
      }
      setData(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <button className="cdc-toggle-btn" onClick={toggle}>
        ⚡ {open ? "Hide" : "Check"} fast-track (Complying Development) eligibility
      </button>
      {open && (
        <div className="cdc-panel">
          {loading && "Checking…"}
          {error && <span style={{ color: "var(--red-600)" }}>{error}</span>}
          {data && (
            <>
              <div className="cdc-panel-row">
                <span>Housing Code (dwelling houses)</span>
                <strong>{data.eligibility.housing_code_zone_eligible ? "Zone eligible" : "Not eligible"}</strong>
              </div>
              <div className="cdc-panel-row">
                <span>Low Rise Housing Diversity Code (dual occ/manor house/terraces)</span>
                <strong>{data.eligibility.low_rise_housing_diversity_code_zone_eligible ? "Zone eligible" : "Not eligible"}</strong>
              </div>
              <div className="cdc-panel-row">
                <span>Lot area</span>
                <strong>{data.eligibility.lot_area_sqm} m²</strong>
              </div>
              <p style={{ marginTop: 8, fontSize: 11.5, color: "var(--gray-500)" }}>
                {data.eligibility.overall_note} This is a separate, faster approval pathway to the
                standard controls shown below — the two use different rules and are not interchangeable.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function AlternateScenarioSection({ scenario }) {
  const [open, setOpen] = useState(false);
  const rules = scenario.rules_applied ?? [];
  const preview = rules.slice(0, 6);

  return (
    <div>
      <CaveatBanner type="info" title="This zone permits more than one building type">
        {scenario.note}
      </CaveatBanner>
      <button className="cdc-toggle-btn" onClick={() => setOpen((o) => !o)}>
        🏢 {open ? "Hide" : "Show"} the {scenario.label.toLowerCase()}
      </button>
      {open && (
        <div className="cdc-panel">
          {scenario.envelope_fallback_warnings?.length > 0 && (
            <p style={{ color: "var(--red-600)", marginBottom: 6 }}>
              {scenario.envelope_fallback_warnings.map((w) => w.message).join(" ")}
            </p>
          )}
          {scenario.envelope_geometry_note && (
            <p style={{ color: "var(--gray-500, #6b7280)", marginBottom: 6, fontSize: "0.9em" }}>
              {scenario.envelope_geometry_note}
            </p>
          )}
          {preview.map((r, i) => {
            const opSym = r.operator === "min" ? "≥" : r.operator === "max" ? "≤" : "=";
            return (
              <div className="cdc-panel-row" key={i}>
                <span>{PARAM_LABELS[r.parameter] || formatParamLabel(r.parameter)}</span>
                <strong>{opSym}&thinsp;{r.value}{r.unit}</strong>
              </div>
            );
          })}
          {rules.length > preview.length && (
            <p style={{ marginTop: 8, fontSize: 11.5, color: "var(--gray-500)" }}>
              +{rules.length - preview.length} more controls under this scenario.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function RuleGroup({ group, rules, activeRule, onRuleClick }) {
  return (
    <section className="rule-group" aria-label={group.label}>
      <div className="group-header">
        {group.icon && (
          <span className="group-icon" style={{ color: group.color }} aria-hidden="true">
            {group.icon}
          </span>
        )}
        <span className="group-label">{group.label}</span>
        <span className="group-count">{rules.length}</span>
      </div>
      {rules.map((rule, i) => {
        const id = group.key + i;
        return (
          <RuleCard
            key={id}
            rule={rule}
            accent={group.color}
            active={activeRule === id}
            onClick={() => onRuleClick(activeRule === id ? null : id)}
          />
        );
      })}
    </section>
  );
}

function RuleCard({ rule, accent, active, onClick }) {
  const label    = PARAM_LABELS[rule.parameter] || formatParamLabel(rule.parameter);
  const opSymbol = rule.operator === "min" ? "≥" : rule.operator === "max" ? "≤" : "=";

  return (
    <button
      className={`rule-card${active ? " rule-card--active" : ""}`}
      style={{ "--accent": accent }}
      onClick={onClick}
      aria-expanded={active}
    >
      <div className="rule-card-bar" style={{ background: accent }} aria-hidden="true" />
      <div className="rule-card-body">
        <div className="rule-row">
          <span className="rule-label">{label}</span>
          <span className="rule-value" style={{ color: accent }}>
            {opSymbol}&thinsp;{rule.value}{rule.unit}
          </span>
        </div>
        {!active && (
          <div className="rule-clause">
            {rule.clause && `${rule.clause} · p.${rule.page}`}
            {rule.variants?.length > 0 && (
              <span className="rule-variants-badge">+{rule.variants.length} variation{rule.variants.length > 1 ? "s" : ""}</span>
            )}
          </div>
        )}
        {active && (
          <div className="rule-detail">
            <span
              className={`rule-source-badge rule-source-badge--${rule.verified ? "verified" : "unverified"}`}
              title={rule.confidence != null ? `Extraction confidence: ${Math.round(rule.confidence * 100)}%` : undefined}
            >
              {rule.verified ? "✓ Human-checked" : "Auto-extracted, not checked"}
            </span>
            {rule.text && (
              <blockquote className="rule-quote">"{rule.text}"</blockquote>
            )}
            {rule.conditions?.length > 0 && (
              <InfoBox title="Applies when" items={rule.conditions} color={accent} />
            )}
            {rule.exceptions?.length > 0 && (
              <InfoBox title="Exceptions" items={rule.exceptions} color="#d97706" />
            )}
            {rule.pdf_link && (
              <a
                href={`${API_BASE}${rule.pdf_link}`}
                target="_blank"
                rel="noreferrer"
                className="pdf-btn"
                onClick={(e) => e.stopPropagation()}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                {rule.clause} — Page {rule.page}
              </a>
            )}
            {rule.variants?.length > 0 && (
              <div className="rule-variants">
                <div className="rule-variants-title">Conditional variations</div>
                {rule.variants.map((v, i) => {
                  const opSym = v.operator === "min" ? "≥" : v.operator === "max" ? "≤" : "=";
                  return (
                    <div key={i} className="rule-variant-row">
                      <span className="rule-variant-value">{opSym}&thinsp;{v.value}{v.unit}</span>
                      <span className="rule-variant-conditions">
                        {v.conditions?.length > 0 ? v.conditions.map(sentenceCase).join("; ") : v.clause}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
      <div className="rule-chevron" aria-hidden="true">
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2.5"
          style={{ transform: active ? "rotate(180deg)" : "rotate(0deg)", transition: "transform 0.2s ease" }}
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </div>
    </button>
  );
}

function MetricPill({ label, value, color }) {
  return (
    <div className="metric-pill">
      <span className="metric-label">{label}</span>
      <span className="metric-value" style={color ? { color } : {}}>{value}</span>
    </div>
  );
}

function InfoBox({ title, items, color }) {
  return (
    <div className="info-box" style={{ "--accent": color }}>
      <span className="info-box-title" style={{ color }}>{title}</span>
      <ul className="info-box-list">
        {items.map((item, i) => <li key={i}>{sentenceCase(item)}</li>)}
      </ul>
    </div>
  );
}
