import { useState, useEffect, useRef } from "react";
import { MapContainer, TileLayer, Polygon, useMap } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import "./App.css";

/* ─────────────────────────── constants ─────────────────────────── */

const PARAM_LABELS = {
  front_setback: "Front Setback",
  rear_setback: "Rear Setback",
  rear_setback_upper: "Rear Setback (Upper)",
  side_setback_ground: "Side Setback (Ground)",
  side_setback_upper: "Side Setback (Upper)",
  max_height: "Max Height",
  max_storeys: "Max Storeys",
  height_plane: "Height Plane",
  landscaped_area_pct: "Landscaped Area",
  private_open_space: "Private Open Space",
  private_open_space_min_dimension: "Open Space Min Dim.",
};

const GROUPS = [
  {
    key: "setbacks",
    label: "Setbacks",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M21 3H3v18h18V3z" /><path d="M9 3v18M3 9h6M3 15h6" />
      </svg>
    ),
    color: "#6366f1",
    match: (p) => p.includes("setback"),
  },
  {
    key: "height",
    label: "Height Controls",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M3 20h18M7 20V10l5-7 5 7v10M9 20v-5h6v5" />
      </svg>
    ),
    color: "#f59e0b",
    match: (p) => ["max_height", "max_storeys", "height_plane"].includes(p),
  },
  {
    key: "landscaping",
    label: "Landscaping & Open Space",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M12 22V12M12 12C12 7 7 3 3 3c0 4 3 9 9 9zM12 12c0-5 5-9 9-9-0 4-3 9-9 9z" />
      </svg>
    ),
    color: "#10b981",
    match: (p) => p.includes("landscaped") || p.includes("open_space"),
  },
];

/* ─────────────────────────── map helper ─────────────────────────── */

function MapUpdater({ bounds }) {
  const map = useMap();
  useEffect(() => {
    if (bounds) map.fitBounds(bounds, { padding: [48, 48] });
  }, [bounds, map]);
  return null;
}

/* ─────────────────────────── main app ─────────────────────────── */

export default function App() {
  const [address, setAddress] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [activeRule, setActiveRule] = useState(null);
  const inputRef = useRef(null);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!address.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setActiveRule(null);
    try {
      const res = await fetch("/envelope", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: address.trim() }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Server error");
      }
      setResult(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const toLeaflet = (coords) => coords.map(([lng, lat]) => [lat, lng]);

  const lotCoords = result?.lot_polygon?.coordinates?.[0]
    ? toLeaflet(result.lot_polygon.coordinates[0])
    : null;

  const envelopeCoords = result?.envelope?.coordinates?.[0]
    ? toLeaflet(result.envelope.coordinates[0])
    : null;

  const mapBounds = lotCoords
    ? [
        [Math.min(...lotCoords.map((p) => p[0])), Math.min(...lotCoords.map((p) => p[1]))],
        [Math.max(...lotCoords.map((p) => p[0])), Math.max(...lotCoords.map((p) => p[1]))],
      ]
    : null;

  return (
    <div className="root">
      {/* ── Header ── */}
      <header className="header">
        <div className="brand">
          <div className="brand-icon-wrap">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2">
              <path d="M12 3L4 7v5c0 5.25 3.5 10.15 8 11.35C16.5 22.15 20 17.25 20 12V7l-8-4z" />
            </svg>
          </div>
          <span className="brand-name">Compliance Copilot</span>
          <span className="brand-tag">NSW DCP</span>
        </div>

        <form onSubmit={handleSubmit} className="search-form">
          <div className="search-wrap">
            <svg className="search-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="8" /><path d="m21 21-4.35-4.35" />
            </svg>
            <input
              ref={inputRef}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="Enter a Sydney address…"
              className="search-input"
              disabled={loading}
            />
            {address && (
              <button type="button" className="clear-btn" onClick={() => { setAddress(""); inputRef.current?.focus(); }}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
          <button type="submit" disabled={loading} className="submit-btn">
            {loading ? <span className="spinner" /> : "Analyse"}
          </button>
        </form>
      </header>

      {/* ── Body ── */}
      <div className="body">
        {/* Map */}
        <div className="map-wrap">
          <MapContainer center={[-33.86, 151.1]} zoom={14} style={{ height: "100%", width: "100%" }} zoomControl={false}>
            <TileLayer
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
              attribution='&copy; <a href="https://carto.com/">CARTO</a>'
            />
            {lotCoords && (
              <Polygon
                positions={lotCoords}
                pathOptions={{ color: "#818cf8", fillColor: "#6366f1", fillOpacity: 0.18, weight: 2, opacity: 0.9 }}
              />
            )}
            {envelopeCoords && (
              <Polygon
                positions={envelopeCoords}
                pathOptions={{ color: "#34d399", fillColor: "#10b981", fillOpacity: 0.22, weight: 2, dashArray: "7 4", opacity: 0.9 }}
              />
            )}
            {mapBounds && <MapUpdater bounds={mapBounds} />}
          </MapContainer>

          {result && (
            <div className="map-legend">
              <div className="legend-row">
                <span className="legend-swatch" style={{ background: "#6366f1", opacity: 0.85 }} />
                <span>Lot boundary</span>
              </div>
              <div className="legend-row">
                <span className="legend-swatch dashed" style={{ borderColor: "#34d399" }} />
                <span>Buildable envelope</span>
              </div>
            </div>
          )}
        </div>

        {/* Panel */}
        <aside className="panel">
          {!result && !loading && !error && <EmptyState onExample={() => { setAddress("35 Connecticut Avenue Five Dock 2046"); inputRef.current?.focus(); }} />}
          {loading && <LoadingState />}
          {error && <ErrorState message={error} onDismiss={() => setError(null)} />}
          {result && (
            <ResultPanel result={result} activeRule={activeRule} onRuleClick={setActiveRule} />
          )}
        </aside>
      </div>
    </div>
  );
}

/* ─────────────────────────── panel states ─────────────────────────── */

function EmptyState({ onExample }) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.5">
          <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <polyline points="9 22 9 12 15 12 15 22" />
        </svg>
      </div>
      <h3 className="empty-title">Search an address</h3>
      <p className="empty-body">
        Enter any Sydney address to calculate the buildable envelope and fetch
        all applicable DCP development controls.
      </p>
      <button className="example-btn" onClick={onExample}>
        Try an example →
      </button>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="loading-state">
      <div className="pulse-ring" />
      <p className="loading-title">Computing envelope…</p>
      <p className="loading-sub">Fetching lot + zoning data from NSW APIs</p>
      {["Geocoding address", "Retrieving lot polygon", "Applying DCP rules"].map((step, i) => (
        <div key={i} className="loading-step" style={{ animationDelay: `${i * 0.4}s` }}>
          <span className="step-dot" />
          {step}
        </div>
      ))}
    </div>
  );
}

function ErrorState({ message, onDismiss }) {
  return (
    <div className="error-state">
      <div className="error-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#f87171" strokeWidth="2">
          <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
        </svg>
      </div>
      <p className="error-title">Something went wrong</p>
      <p className="error-msg">{message}</p>
      <button className="dismiss-btn" onClick={onDismiss}>Dismiss</button>
    </div>
  );
}

/* ─────────────────────────── result panel ─────────────────────────── */

function ResultPanel({ result, activeRule, onRuleClick }) {
  const rules = result.rules_applied ?? [];

  return (
    <div className="result-panel">
      {/* Address card */}
      <div className="address-card">
        <div className="address-line">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
            <circle cx="12" cy="10" r="3" />
          </svg>
          <span className="address-text">{result.address}</span>
        </div>
        <div className="meta-row">
          <span className="zone-badge">Zone {result.zone}</span>
          <span className="rule-count">{rules.length} controls applied</span>
        </div>
      </div>

      {/* Rule groups */}
      {GROUPS.map((group) => {
        const grouped = rules.filter((r) => group.match(r.parameter));
        const other = rules.filter(
          (r) => !GROUPS.some((g) => g.match(r.parameter))
        );
        const list = group.key === "setbacks" ? grouped
          : group.key === "height" ? grouped
          : group.key === "landscaping" ? grouped
          : other;
        if (list.length === 0) return null;
        return (
          <RuleGroup
            key={group.key}
            group={group}
            rules={list}
            activeRule={activeRule}
            onRuleClick={onRuleClick}
          />
        );
      })}

      {/* Other rules not in any group */}
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

function RuleGroup({ group, rules, activeRule, onRuleClick }) {
  return (
    <div className="rule-group">
      <div className="group-header" style={{ "--accent": group.color }}>
        {group.icon && <span className="group-icon" style={{ color: group.color }}>{group.icon}</span>}
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
    </div>
  );
}

function RuleCard({ rule, accent, active, onClick }) {
  const label = PARAM_LABELS[rule.parameter] || rule.parameter;
  const opSymbol = rule.operator === "min" ? "≥" : rule.operator === "max" ? "≤" : "=";

  return (
    <div
      className={`rule-card ${active ? "rule-card--active" : ""}`}
      style={{ "--accent": accent }}
      onClick={onClick}
    >
      <div className="rule-card-bar" style={{ background: accent }} />

      <div className="rule-card-body">
        <div className="rule-row">
          <span className="rule-label">{label}</span>
          <span className="rule-value" style={{ color: accent }}>
            {opSymbol}&thinsp;{rule.value}{rule.unit}
          </span>
        </div>

        {!active && rule.clause && (
          <div className="rule-clause">{rule.clause} · p.{rule.page}</div>
        )}

        {active && (
          <div className="rule-detail">
            {rule.text && (
              <blockquote className="rule-quote">"{rule.text}"</blockquote>
            )}

            {rule.conditions?.length > 0 && (
              <InfoBox title="Conditions" items={rule.conditions} color="#6366f1" />
            )}
            {rule.exceptions?.length > 0 && (
              <InfoBox title="Exceptions" items={rule.exceptions} color="#f59e0b" />
            )}

            <a href={rule.pdf_link} target="_blank" rel="noreferrer" className="pdf-btn">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              {rule.clause} — Page {rule.page}
            </a>
          </div>
        )}
      </div>

      <div className="rule-chevron">
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2.5"
          style={{ transform: active ? "rotate(180deg)" : "rotate(0deg)", transition: "transform 0.2s" }}
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </div>
    </div>
  );
}

function InfoBox({ title, items, color }) {
  return (
    <div className="info-box" style={{ "--accent": color }}>
      <span className="info-box-title" style={{ color }}>{title}</span>
      <ul className="info-box-list">
        {items.map((item, i) => <li key={i}>{item}</li>)}
      </ul>
    </div>
  );
}

/* ─────────────────────────── global styles ─────────────────────────── */

const css = `
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body, #root { height: 100%; }
  body { font-family: 'Inter', system-ui, sans-serif; background: #070b14; }

  /* scrollbar */
  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #1e2d3d; border-radius: 10px; }

  /* ── layout ── */
  .root { display: flex; flex-direction: column; height: 100vh; overflow: hidden; color: #e2e8f0; }

  /* ── header ── */
  .header {
    display: flex; align-items: center; gap: 20px;
    padding: 0 20px; height: 56px;
    background: #070b14;
    border-bottom: 1px solid #0f1f30;
    flex-shrink: 0; z-index: 50;
  }
  .brand { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  .brand-icon-wrap {
    width: 32px; height: 32px; border-radius: 8px;
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 14px rgba(99,102,241,0.45);
  }
  .brand-name { font-size: 15px; font-weight: 700; color: #f1f5f9; letter-spacing: -0.2px; }
  .brand-tag {
    font-size: 10px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase;
    color: #6366f1; background: rgba(99,102,241,0.12); border: 1px solid rgba(99,102,241,0.3);
    padding: 2px 7px; border-radius: 20px;
  }

  /* search */
  .search-form { display: flex; gap: 8px; flex: 1; max-width: 600px; }
  .search-wrap {
    flex: 1; display: flex; align-items: center;
    background: #0d1728; border: 1px solid #1a2d40;
    border-radius: 10px; padding: 0 12px; gap: 8px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .search-wrap:focus-within {
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.15);
  }
  .search-icon { color: #475569; flex-shrink: 0; }
  .search-input {
    flex: 1; background: transparent; border: none; outline: none;
    font-size: 13.5px; color: #cbd5e1; caret-color: #6366f1;
    font-family: inherit; padding: 9px 0;
  }
  .search-input::placeholder { color: #334155; }
  .clear-btn {
    background: none; border: none; cursor: pointer; color: #475569;
    display: flex; align-items: center; padding: 2px; border-radius: 4px;
  }
  .clear-btn:hover { color: #94a3b8; }
  .submit-btn {
    padding: 0 22px; height: 38px;
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
    color: white; border: none; border-radius: 10px;
    font-weight: 600; font-size: 13.5px; font-family: inherit;
    cursor: pointer; white-space: nowrap; flex-shrink: 0;
    box-shadow: 0 0 16px rgba(99,102,241,0.35);
    transition: opacity 0.15s, box-shadow 0.15s;
    display: flex; align-items: center; justify-content: center;
  }
  .submit-btn:hover:not(:disabled) { opacity: 0.88; box-shadow: 0 0 22px rgba(99,102,241,0.5); }
  .submit-btn:disabled { opacity: 0.6; cursor: not-allowed; }

  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    display: inline-block; width: 15px; height: 15px;
    border: 2px solid rgba(255,255,255,0.25);
    border-top-color: white; border-radius: 50%;
    animation: spin 0.65s linear infinite;
  }

  /* ── body ── */
  .body { display: flex; flex: 1; overflow: hidden; }

  /* map */
  .map-wrap { flex: 1; position: relative; }
  .map-legend {
    position: absolute; bottom: 20px; left: 16px; z-index: 1000;
    background: rgba(7,11,20,0.82); backdrop-filter: blur(10px);
    border: 1px solid #1a2d40; border-radius: 10px;
    padding: 10px 14px; display: flex; flex-direction: column; gap: 7px;
  }
  .legend-row { display: flex; align-items: center; gap: 8px; font-size: 11px; color: #94a3b8; }
  .legend-swatch {
    width: 22px; height: 3px; border-radius: 10px; flex-shrink: 0;
  }
  .legend-swatch.dashed {
    background: transparent; border-top: 2px dashed;
  }

  /* leaflet tweaks */
  .leaflet-control-attribution { background: rgba(7,11,20,0.75) !important; color: #334155 !important; }
  .leaflet-control-attribution a { color: #475569 !important; }
  .leaflet-control-zoom { border: 1px solid #1a2d40 !important; border-radius: 8px !important; overflow: hidden; }
  .leaflet-control-zoom a {
    background: #0d1728 !important; color: #94a3b8 !important;
    border-bottom: 1px solid #1a2d40 !important;
  }
  .leaflet-control-zoom a:hover { background: #162032 !important; color: #cbd5e1 !important; }

  /* ── panel ── */
  .panel {
    width: 400px; flex-shrink: 0;
    background: #070b14;
    border-left: 1px solid #0f1f30;
    overflow-y: auto; display: flex; flex-direction: column;
  }

  /* ── empty ── */
  .empty-state {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; flex: 1; padding: 40px 28px; text-align: center;
    gap: 12px;
  }
  .empty-icon {
    width: 60px; height: 60px; border-radius: 16px;
    background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.2);
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 4px;
  }
  .empty-title { font-size: 15px; font-weight: 600; color: #e2e8f0; }
  .empty-body { font-size: 13px; color: #475569; line-height: 1.65; max-width: 280px; }
  .example-btn {
    margin-top: 8px; padding: 8px 18px;
    background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3);
    border-radius: 8px; color: #818cf8; font-size: 12.5px; font-weight: 600;
    font-family: inherit; cursor: pointer; transition: background 0.15s;
  }
  .example-btn:hover { background: rgba(99,102,241,0.18); }

  /* ── loading ── */
  @keyframes pulse-ring {
    0% { transform: scale(0.8); opacity: 0.5; }
    100% { transform: scale(1.4); opacity: 0; }
  }
  @keyframes fade-up {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .loading-state {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; flex: 1; padding: 40px 28px; gap: 8px;
  }
  .pulse-ring {
    width: 52px; height: 52px; border-radius: 50%;
    background: rgba(99,102,241,0.2); border: 2px solid #6366f1;
    animation: pulse-ring 1.2s ease-out infinite;
    margin-bottom: 12px;
  }
  .loading-title { font-size: 15px; font-weight: 600; color: #e2e8f0; }
  .loading-sub { font-size: 12px; color: #475569; margin-bottom: 8px; }
  .loading-step {
    font-size: 12px; color: #64748b; display: flex; align-items: center; gap: 8px;
    animation: fade-up 0.4s ease forwards; opacity: 0;
  }
  .step-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #6366f1; flex-shrink: 0;
    box-shadow: 0 0 6px rgba(99,102,241,0.6);
  }

  /* ── error ── */
  .error-state {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; flex: 1; padding: 40px 28px; gap: 10px; text-align: center;
  }
  .error-icon {
    width: 52px; height: 52px; border-radius: 14px;
    background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25);
    display: flex; align-items: center; justify-content: center;
  }
  .error-title { font-size: 15px; font-weight: 600; color: #fca5a5; }
  .error-msg { font-size: 12.5px; color: #64748b; max-width: 280px; line-height: 1.6; }
  .dismiss-btn {
    margin-top: 6px; padding: 7px 18px;
    background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25);
    border-radius: 8px; color: #f87171; font-size: 12.5px; font-weight: 600;
    font-family: inherit; cursor: pointer; transition: background 0.15s;
  }
  .dismiss-btn:hover { background: rgba(239,68,68,0.18); }

  /* ── result panel ── */
  .result-panel { display: flex; flex-direction: column; padding: 16px; gap: 4px; }

  .address-card {
    background: #0d1728; border: 1px solid #1a2d40;
    border-radius: 12px; padding: 14px 16px; margin-bottom: 12px;
  }
  .address-line { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 10px; }
  .address-text { font-size: 13px; font-weight: 500; color: #cbd5e1; line-height: 1.5; }
  .meta-row { display: flex; align-items: center; gap: 8px; }
  .zone-badge {
    padding: 3px 10px; border-radius: 20px;
    background: linear-gradient(135deg, rgba(99,102,241,0.2), rgba(139,92,246,0.2));
    border: 1px solid rgba(99,102,241,0.35);
    font-size: 11px; font-weight: 700; color: #a5b4fc; letter-spacing: 0.4px;
  }
  .rule-count { font-size: 11px; color: #475569; }

  /* ── rule group ── */
  .rule-group { margin-bottom: 16px; }
  .group-header {
    display: flex; align-items: center; gap: 7px;
    padding: 0 4px; margin-bottom: 8px;
  }
  .group-icon { display: flex; align-items: center; opacity: 0.9; }
  .group-label { font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.8px; flex: 1; }
  .group-count {
    font-size: 10px; font-weight: 600;
    background: #0d1728; border: 1px solid #1a2d40;
    color: #475569; border-radius: 20px; padding: 1px 7px;
  }

  /* ── rule card ── */
  .rule-card {
    display: flex; align-items: stretch;
    background: #0a1220; border: 1px solid #0f1f30;
    border-radius: 10px; margin-bottom: 5px;
    cursor: pointer; overflow: hidden;
    transition: border-color 0.15s, background 0.15s;
  }
  .rule-card:hover { background: #0d1728; border-color: #1a2d40; }
  .rule-card--active {
    background: #0d1728; border-color: var(--accent, #6366f1);
    box-shadow: 0 0 0 1px var(--accent, #6366f1), inset 0 0 30px rgba(99,102,241,0.04);
  }
  .rule-card-bar { width: 3px; flex-shrink: 0; opacity: 0.7; border-radius: 10px 0 0 10px; }
  .rule-card-body { flex: 1; padding: 11px 12px; min-width: 0; }
  .rule-row { display: flex; align-items: center; gap: 8px; }
  .rule-label { font-size: 13px; font-weight: 500; color: #94a3b8; flex: 1; }
  .rule-value { font-size: 13px; font-weight: 700; flex-shrink: 0; }
  .rule-clause { font-size: 11px; color: #334155; margin-top: 4px; }
  .rule-chevron { display: flex; align-items: center; padding: 0 10px; color: #334155; flex-shrink: 0; }

  /* rule detail */
  @keyframes slide-down {
    from { opacity: 0; transform: translateY(-6px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .rule-detail {
    margin-top: 12px; padding-top: 12px;
    border-top: 1px solid #0f1f30;
    display: flex; flex-direction: column; gap: 10px;
    animation: slide-down 0.2s ease;
  }
  .rule-quote {
    font-size: 12px; color: #64748b; line-height: 1.6;
    font-style: italic; padding-left: 10px;
    border-left: 2px solid #1a2d40;
    margin: 0;
  }
  .info-box {
    background: #070b14; border: 1px solid #0f1f30;
    border-left: 2px solid var(--accent, #6366f1);
    border-radius: 6px; padding: 8px 10px;
  }
  .info-box-title { font-size: 10px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase; display: block; margin-bottom: 5px; }
  .info-box-list { list-style: none; display: flex; flex-direction: column; gap: 3px; }
  .info-box-list li { font-size: 11.5px; color: #64748b; line-height: 1.5; }
  .info-box-list li::before { content: "·  "; color: #334155; }

  .pdf-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 12px; border-radius: 7px;
    background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.25);
    color: #818cf8; font-size: 12px; font-weight: 600; text-decoration: none;
    transition: background 0.15s; align-self: flex-start;
  }
  .pdf-btn:hover { background: rgba(99,102,241,0.18); }
`;

const el = document.createElement("style");
el.textContent = css;
document.head.appendChild(el);
