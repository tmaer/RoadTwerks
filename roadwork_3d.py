"""
3D BEFORE / AFTER view for RoadWork AI.

For the selected camera, takes every sensor within RADIUS_KM, builds its typical
hourly traffic, and compares closing 1 of 3 lanes at the BUSIEST daytime hour
(before) vs the QUIETEST daytime hour (after). Columns morph smoothly on the GPU
(deck.gl over OpenStreetMap tiles), with the same colour scale in both states.

Usage in app.py:
    from roadwork_3d import render_3d_comparison
    render_3d_comparison(sensors, selected_id, load_hourly_profiles)
    # load_hourly_profiles(ids) -> DataFrame[sensor_id, h, vph] (vehicles/hour by hour of day)
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from roadworkwise_core import travel_minutes

RADIUS_KM = 1.5          # sensors around the selected camera
MAX_SENSORS = 80
TOTAL_LANES, CLOSED_LANES = 3, 1
SECTION_KM = 1.0
DAY_HOURS = range(6, 22)  # works considered between 06:00 and 22:00


def _km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def build_data(sensors, selected_id, load_profiles):
    cam = sensors.loc[sensors["sensor_id"] == selected_id].iloc[0]
    s = sensors.copy()
    s["dist"] = _km(cam["Latitude"], cam["Longitude"], s["Latitude"], s["Longitude"])
    near = s[s["dist"] <= RADIUS_KM].nsmallest(MAX_SENSORS, "dist")

    prof = load_profiles(tuple(int(i) for i in near["sensor_id"]))
    if prof is None or prof.empty:
        return None
    day = prof[prof["h"].isin(DAY_HOURS)]
    totals = day.groupby("h")["vph"].sum()
    if totals.empty:
        return None
    h_before, h_after = int(totals.idxmax()), int(totals.idxmin())

    def state(h):
        v = prof[prof["h"] == h].set_index("sensor_id")["vph"]
        t_n, _ = travel_minutes(v, TOTAL_LANES, SECTION_KM)
        t_w, _ = travel_minutes(v, TOTAL_LANES - CLOSED_LANES, SECTION_KM)
        return v, (t_w - t_n)

    vb, eb = state(h_before)
    va, ea = state(h_after)
    rows = []
    for r in near.itertuples():
        sid = r.sensor_id
        if sid not in vb.index or sid not in va.index:
            continue
        rows.append({"n": str(r.Name or f"Camera {sid}"), "id": int(sid),
                     "sel": bool(sid == selected_id),
                     "p": [float(r.Longitude), float(r.Latitude)],
                     "vb": float(vb[sid]), "va": float(va[sid]),
                     "eb": float(eb[sid]), "ea": float(ea[sid])})
    if not rows:
        return None
    lost_b = sum(r["eb"] * r["vb"] / 60 for r in rows)
    lost_a = sum(r["ea"] * r["va"] / 60 for r in rows)
    return {
        "rows": rows, "hourBefore": h_before, "hourAfter": h_after,
        "maxExtraBefore": max(r["eb"] for r in rows), "maxExtraAfter": max(r["ea"] for r in rows),
        "saving": round(100 * (1 - lost_a / lost_b)) if lost_b else 0,
        "center": [float(cam["Longitude"]), float(cam["Latitude"])],
    }


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/deck.gl@9.0.38/dist.min.js"></script>
<style>
  html,body{margin:0;height:100%;background:#0B1726;font-family:Inter,system-ui,sans-serif;color:#fff;overflow:hidden}
  #map{position:absolute;inset:0}
  .card{position:absolute;background:rgba(11,23,38,.82);backdrop-filter:blur(8px);
        border:1px solid rgba(255,255,255,.08);border-radius:14px;z-index:2}
  .panel{top:16px;left:16px;padding:14px 18px;min-width:250px}
  .k{font-size:11px;letter-spacing:.14em;color:#8FA3BF;text-transform:uppercase}
  .state{font-size:30px;font-weight:800;margin:6px 0 0;transition:color .6s}
  .sub{font-size:13px;color:#C7D2E0;margin-top:2px}
  .stat{margin-top:10px;font-size:12px;color:#8FA3BF}.stat b{display:block;font-size:22px;color:#fff}
  button{all:unset;cursor:pointer;margin-top:12px;padding:8px 12px;border-radius:9px;
         background:#F5A524;color:#0B1726;font-weight:700;font-size:13px}
  .saving{top:16px;right:16px;padding:12px 18px;text-align:right;opacity:0;transition:opacity .8s;font-size:12px;color:#C7D2E0}
  .saving b{display:block;font-size:40px;line-height:1.1;color:#3DDC97}
  .legend{bottom:16px;left:16px;padding:10px 14px;font-size:11px;color:#C7D2E0}
  .bar{width:200px;height:8px;border-radius:4px;margin:6px 0 3px;
       background:linear-gradient(90deg,#1A9850,#91CF60,#D9EF8B,#FEE08B,#FC8D59,#D73027)}
  .row{display:flex;justify-content:space-between}
  .attr{position:absolute;right:8px;bottom:6px;font-size:10px;color:#8FA3BF;z-index:2}
  .attr a{color:#8FA3BF}
</style></head><body>
<div id="map"></div>
<div class="card panel"><div class="k">Lane closure · 1 of 3 lanes</div>
  <div class="state" id="state">BEFORE</div><div class="sub" id="sub"></div>
  <div class="stat">Max extra delay per trip<b id="delay"></b></div>
  <button id="toggle">Show AFTER ▸</button></div>
<div class="card saving" id="saving">Time lost by drivers<b id="pct"></b>less</div>
<div class="card legend">Extra delay if a lane is closed<div class="bar"></div>
  <div class="row"><span>none</span><span>severe</span></div>
  <div style="margin-top:6px">Height = vehicles per hour · wider column = selected camera</div></div>
<div class="attr">© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors</div>
<script>
const D = __DATA__;
const PAL=[[26,152,80],[145,207,96],[217,239,139],[254,224,139],[252,141,89],[215,48,39]];
const maxE=Math.max(D.maxExtraBefore,D.maxExtraAfter)||1;
const maxV=Math.max(...D.rows.map(r=>Math.max(r.vb,r.va)))||1;
const ELEV=520/maxV, ease=t=>t*t*(3-2*t);
function color(v){const x=Math.min(Math.max(v/maxE,0),1)*(PAL.length-1);
  const i=Math.min(Math.floor(x),PAL.length-2),f=x-i;
  return PAL[i].map((c,k)=>Math.round(c+(PAL[i+1][k]-c)*f)).concat(240);}
let after=false,autoplay=true,spin=true;
const hh=h=>String(h).padStart(2,"0")+":00";
const fmt=m=>m<1?`+${Math.round(m*60)} s`:`+${m.toFixed(1)} min`;

// dark street map: OpenStreetMap raster tiles, desaturated and tinted navy
const basemap=new deck.TileLayer({id:"osm",data:"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  minZoom:0,maxZoom:19,tileSize:256,
  renderSubLayers:p=>{const [[w,s],[e,n]]=p.tile.boundingBox;
    return new deck.BitmapLayer(p,{data:null,image:p.data,bounds:[w,s,e,n],desaturate:1,tintColor:[70,92,125]});}});

function layers(){const tr={duration:1800,easing:ease};
  const common={diskResolution:28,extruded:true,pickable:true,
    getPosition:r=>r.p,getElevation:r=>(after?r.va:r.vb)*ELEV,getFillColor:r=>color(after?r.ea:r.eb),
    material:{ambient:.5,diffuse:.6,shininess:40},
    updateTriggers:{getElevation:after,getFillColor:after},transitions:{getElevation:tr,getFillColor:tr}};
  return [basemap,
    new deck.ColumnLayer({id:"cols",data:D.rows.filter(r=>!r.sel),radius:26,...common}),
    new deck.ColumnLayer({id:"sel",data:D.rows.filter(r=>r.sel),radius:48,...common})];}

let vs={longitude:D.center[0],latitude:D.center[1],zoom:14.4,pitch:58,bearing:-20,maxPitch:75};
const dg=new deck.DeckGL({container:"map",viewState:vs,controller:true,layers:layers(),
  onViewStateChange:({viewState,interactionState})=>{vs=viewState;
    if(interactionState&&(interactionState.isDragging||interactionState.isZooming||interactionState.isRotating))spin=false;
    dg.setProps({viewState:vs});},
  getTooltip:({object:r})=>r&&`${r.n} (ID ${r.id})\n${Math.round(after?r.va:r.vb)} veh/h · ${fmt(after?r.ea:r.eb)}`});

function render(){dg.setProps({layers:layers()});
  const s=document.getElementById("state");s.textContent=after?"AFTER":"BEFORE";s.style.color=after?"#3DDC97":"#FC8D59";
  document.getElementById("sub").textContent=after?`Works at ${hh(D.hourAfter)} · quietest daytime hour`:`Works at ${hh(D.hourBefore)} · busiest daytime hour`;
  document.getElementById("delay").textContent=fmt(after?D.maxExtraAfter:D.maxExtraBefore);
  document.getElementById("pct").textContent=`−${D.saving}%`;
  document.getElementById("saving").style.opacity=after?1:0;
  document.getElementById("toggle").textContent=after?"◂ Show BEFORE":"Show AFTER ▸";}
function toggle(){after=!after;render();}
document.getElementById("toggle").onclick=()=>{autoplay=false;toggle();};
setInterval(()=>{if(autoplay)toggle();},4500);
(function rot(){if(spin){vs={...vs,bearing:vs.bearing+0.03};dg.setProps({viewState:vs});}requestAnimationFrame(rot);})();
render();
</script></body></html>"""


def render_3d_comparison(sensors, selected_id, load_profiles, height=600):
    """Draws the 3D component. Returns the data dict (or None if not enough data).

    The page is written to ./static and embedded by URL (needs
    `[server] enableStaticServing = true` in .streamlit/config.toml). A real page
    (not an inline srcdoc iframe) lets MapLibre start its web workers, so the
    streets and 3D buildings render.
    """
    try:
        data = build_data(sensors, selected_id, load_profiles)
    except Exception:
        return None
    if data is None:
        return None
    html = HTML.replace("__DATA__", json.dumps(data))
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(exist_ok=True)
    name = f"rw3d_{int(selected_id)}.html"
    (static_dir / name).write_text(html, encoding="utf-8")
    version = hashlib.md5(html.encode("utf-8")).hexdigest()[:8]   # cache-buster
    st.iframe(f"/app/static/{name}?v={version}", height=height)
    return data
