"""
Step 7 — Build a smooth 3D BEFORE/AFTER page (GPU-animated, runs at your screen's Hz).

Input:  demo_before.csv, demo_after.csv  (from 5_demo_3d.py)
Output: roadworkwise_3d.html  -> double-click to open in the browser (needs internet for the map)

Controls on the page:
  SPACE or click  -> toggle BEFORE / AFTER
  P               -> auto-play loop on/off
  R               -> slow camera rotation on/off
  Mouse: drag = move, right-drag = tilt/rotate, wheel = zoom
"""
import json
import pandas as pd

AVENUE_LABEL = "Paseo de la Castellana, Madrid"

keys = ["name", "latitude", "longitude"]
b = pd.read_csv("demo_before.csv")
a = pd.read_csv("demo_after.csv")
m = b[keys + ["vehicles", "extra_min"]].merge(
    a[keys + ["vehicles", "extra_min"]], on=keys, suffixes=("_b", "_a"))

lost_b = float((m["extra_min_b"] * m["vehicles_b"] / 60).sum())
lost_a = float((m["extra_min_a"] * m["vehicles_a"] / 60).sum())
data = {
    "rows": [{"n": r.name, "p": [r.longitude, r.latitude],
              "vb": r.vehicles_b, "va": r.vehicles_a,
              "eb": r.extra_min_b, "ea": r.extra_min_a} for r in m.itertuples()],
    "hourBefore": int(b["hour"].iloc[0]), "hourAfter": int(a["hour"].iloc[0]),
    "maxExtraBefore": float(m["extra_min_b"].max()), "maxExtraAfter": float(m["extra_min_a"].max()),
    "saving": round(100 * (1 - lost_a / lost_b)) if lost_b else 0,
    "center": [float(m["longitude"].mean()), float(m["latitude"].mean())],
    "label": AVENUE_LABEL,
}

HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>RoadworkWise 3D</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/deck.gl@9.0.38/dist.min.js"></script>
<style>
  html,body{margin:0;height:100%;background:#0b0e14;font-family:Inter,Segoe UI,Arial,sans-serif;color:#fff}
  #map{position:absolute;inset:0}
  .panel{position:absolute;top:24px;left:24px;background:rgba(10,13,20,.78);backdrop-filter:blur(8px);
         border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px 24px;min-width:340px}
  .brand{font-size:13px;letter-spacing:.18em;color:#8b93a7;text-transform:uppercase}
  .place{font-size:15px;color:#c9cfdb;margin-top:4px}
  .state{font-size:44px;font-weight:800;margin:14px 0 2px;transition:color .6s}
  .sub{font-size:18px;color:#c9cfdb}
  .stat{margin-top:16px;font-size:15px;color:#c9cfdb}
  .stat b{font-size:28px;color:#fff;display:block;transition:color .6s}
  .saving{position:absolute;top:24px;right:24px;text-align:right;background:rgba(10,13,20,.78);
          border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:18px 24px;opacity:0;transition:opacity .8s}
  .saving b{font-size:56px;color:#1A9850;display:block;line-height:1}
  .legend{position:absolute;bottom:28px;left:24px;background:rgba(10,13,20,.78);border-radius:12px;padding:12px 16px;font-size:13px;color:#c9cfdb}
  .bar{width:260px;height:10px;border-radius:5px;margin:8px 0 4px;
       background:linear-gradient(90deg,#1A9850,#91CF60,#D9EF8B,#FEE08B,#FC8D59,#D73027)}
  .row{display:flex;justify-content:space-between}
  .hint{position:absolute;bottom:28px;right:24px;font-size:12px;color:#6b7386}
  button{all:unset;cursor:pointer;margin-top:16px;padding:10px 16px;border-radius:10px;
         background:#1f2633;font-weight:600;font-size:14px}
  button:hover{background:#2a3344}
</style></head>
<body>
<div id="map"></div>
<div class="panel">
  <div class="brand">RoadworkWise</div>
  <div class="place" id="place"></div>
  <div class="state" id="state">BEFORE</div>
  <div class="sub" id="sub"></div>
  <div class="stat">Max extra delay per trip<b id="delay"></b></div>
  <button id="toggle">Show AFTER ▸</button>
</div>
<div class="saving" id="saving">Time lost by drivers<b id="pct"></b>less</div>
<div class="legend">Extra delay if a lane is closed
  <div class="bar"></div><div class="row"><span>none</span><span>severe</span></div>
  <div style="margin-top:8px">Column height = vehicles per hour</div></div>
<div class="hint">SPACE toggle · P autoplay · R rotate</div>
<script>
const D = __DATA__;
const PALETTE = [[26,152,80],[145,207,96],[217,239,139],[254,224,139],[252,141,89],[215,48,39]];
const maxExtra = Math.max(D.maxExtraBefore, D.maxExtraAfter) || 1;
const maxVeh = Math.max(...D.rows.map(r => Math.max(r.vb, r.va))) || 1;
const ELEV = 650 / maxVeh;                       // tallest column ~650 m
const ease = t => t * t * (3 - 2 * t);

function color(v) {                               // shared scale for BEFORE and AFTER
  const x = Math.min(Math.max(v / maxExtra, 0), 1) * (PALETTE.length - 1);
  const i = Math.min(Math.floor(x), PALETTE.length - 2), f = x - i;
  return PALETTE[i].map((c, k) => Math.round(c + (PALETTE[i + 1][k] - c) * f)).concat(235);
}

let after = false, autoplay = true, rotate = true;
const hh = h => String(h).padStart(2, "0") + ":00";

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: D.center, zoom: 14, pitch: 62, bearing: -25, antialias: true
});

map.on("load", () => {
  try {                                           // 3D buildings from the base map tiles
    const src = Object.entries(map.getStyle().sources).find(([, s]) => s.type === "vector")[0];
    map.addLayer({
      id: "3d-buildings", source: src, "source-layer": "building", type: "fill-extrusion", minzoom: 13,
      paint: {
        "fill-extrusion-color": "#2b3140",
        "fill-extrusion-height": ["coalesce", ["get", "render_height"], ["get", "height"], 12],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], ["get", "min_height"], 0],
        "fill-extrusion-opacity": 0.85
      }
    });
  } catch (e) { console.warn("No 3D buildings:", e); }
  render();
});

const overlay = new deck.MapboxOverlay({ interleaved: true, layers: [] });
map.addControl(overlay);

function layer() {
  const tr = { duration: 1800, easing: ease };
  return new deck.ColumnLayer({
    id: "cols", data: D.rows, diskResolution: 24, radius: 38, extruded: true, pickable: true,
    getPosition: r => r.p,
    getElevation: r => (after ? r.va : r.vb) * ELEV,
    getFillColor: r => color(after ? r.ea : r.eb),
    material: { ambient: 0.45, diffuse: 0.6, shininess: 40, specularColor: [255, 255, 255] },
    updateTriggers: { getElevation: after, getFillColor: after },
    transitions: { getElevation: tr, getFillColor: tr }
  });
}

function render() {
  overlay.setProps({
    layers: [layer()],
    getTooltip: ({ object: r }) => r && `${r.n}\n${Math.round(after ? r.va : r.vb)} veh/h · +${(after ? r.ea : r.eb).toFixed(1)} min`
  });
  document.getElementById("place").textContent = D.label;
  document.getElementById("state").textContent = after ? "AFTER" : "BEFORE";
  document.getElementById("state").style.color = after ? "#91CF60" : "#FC8D59";
  document.getElementById("sub").textContent =
    after ? `Works start at ${hh(D.hourAfter)} (RoadworkWise)` : `Works start at ${hh(D.hourBefore)} (rush hour)`;
  document.getElementById("delay").textContent = `+${(after ? D.maxExtraAfter : D.maxExtraBefore).toFixed(1)} min`;
  document.getElementById("pct").textContent = `−${D.saving}%`;
  document.getElementById("saving").style.opacity = after ? 1 : 0;
  document.getElementById("toggle").textContent = after ? "◂ Show BEFORE" : "Show AFTER ▸";
}

function toggle() { after = !after; render(); }
document.getElementById("toggle").onclick = () => { autoplay = false; toggle(); };
addEventListener("keydown", e => {
  if (e.code === "Space") { e.preventDefault(); autoplay = false; toggle(); }
  if (e.key.toLowerCase() === "p") autoplay = !autoplay;
  if (e.key.toLowerCase() === "r") rotate = !rotate;
});
setInterval(() => { if (autoplay) toggle(); }, 4500);
(function spin() { if (rotate) map.setBearing(map.getBearing() + 0.03); requestAnimationFrame(spin); })();
</script></body></html>"""

with open("roadworkwise_3d.html", "w", encoding="utf-8") as f:
    f.write(HTML.replace("__DATA__", json.dumps(data)))

print(f"{len(m)} columns | BEFORE {data['hourBefore']:02d}:00 vs AFTER {data['hourAfter']:02d}:00 | "
      f"saving {data['saving']}%")
print("Open roadworkwise_3d.html in your browser")
