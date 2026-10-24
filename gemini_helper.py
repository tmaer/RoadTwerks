"""best_time.py: least disruptive roadwork window for a map node, explained by Gemini.

Setup: pip install google-genai   |   env: GEMINI_API_KEY (optional: GEMINI_MODEL)
rows: list of dicts like {"latitude": 40.41, "longitude": -3.70, "date": "2026-10-20", "time": "08:15", "vehicle_count": 120}
"""
import json
import os
from datetime import date
from math import asin, cos, radians, sin, sqrt

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def km(lat1, lon1, lat2, lon2):
    """Distance in km between two lat/lon points (haversine)."""
    a = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 12742 * asin(sqrt(a))


def profile(rows, lat, lon, radius_km=0.15):
    """Average vehicle_count per (weekday, hour) slot near the clicked node.
    Returns 168 values, index = weekday*24 + hour (Mon = 0). None = no data."""
    tot, cnt = [0.0] * 168, [0] * 168
    for r in rows:
        if km(lat, lon, float(r["latitude"]), float(r["longitude"])) > radius_km:
            continue
        i = date.fromisoformat(str(r["date"])[:10]).weekday() * 24 + int(str(r["time"]).split(":")[0])
        tot[i] += float(r["vehicle_count"])
        cnt[i] += 1
    return [t / c if c else None for t, c in zip(tot, cnt)]


def _sum(p, i, n):
    s = 0
    for j in range(n):
        v = p[(i + j) % 168]
        if v is None:
            return float("inf")
        s += v
    return s


def _label(i, n):
    return {"day": DAYS[i // 24], "start": f"{i % 24:02d}:00", "end": f"{(i + n) % 24:02d}:00"}


def candidates(p, n):
    """Top 3 non-overlapping windows of n hours with the lowest total traffic."""
    idx = sorted((i for i in range(168) if _sum(p, i, n) != float("inf")), key=lambda i: _sum(p, i, n))
    pick = []
    for i in idx:
        if all(min((i - q) % 168, (q - i) % 168) >= n for q in pick):
            pick.append(i)
        if len(pick) == 3:
            break
    peak = max((_sum(p, i, n) for i in idx), default=0)
    return [{**_label(i, n),
             "avgVehiclesPerHour": round(_sum(p, i, n) / n),
             "lessThanPeakPct": round(100 * (1 - _sum(p, i, n) / peak))} for i in pick]


def best_time(rows, lat, lon, hours=4, radius_km=0.15):
    p = profile(rows, lat, lon, radius_km)
    cand = candidates(p, hours)
    if not cand:
        return {"error": "Not enough data near this node."}
    fallback = {"best_window": cand[0], "alternatives": cand[1:], "source": "data-only"}  # used if Gemini is missing or fails
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return fallback

    from google import genai  # imported here so the module still works without the package in data-only mode
    client = genai.Client(api_key=key)
    table = [{"day": d, "vehiclesPerHour": [None if v is None else round(v) for v in p[k * 24:k * 24 + 24]]} for k, d in enumerate(DAYS)]
    prompt = f"""You help a city planner pick the least disruptive {hours}-hour window for roadwork. Average vehicles per hour by weekday (24 values, hours 0 to 23, null = no data): {json.dumps(table)}.
Candidate windows computed from the data, lowest traffic first: {json.dumps(cand)}.
Pick the window with the lowest traffic (normally the first candidate) and explain why it beats the others in plain English.
Return ONLY JSON: {{"best_window":{{"day":"","start":"","end":"","reason":""}},"alternatives":[{{"day":"","start":"","end":"","reason":""}}],"summary":"two sentences for the planner"}}"""

    for _ in range(2):  # one retry on bad output
        try:
            r = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            j = json.loads(r.text)
            if "best_window" in j:
                j["source"] = "gemini"
                j["agreesWithData"] = j["best_window"].get("day") == cand[0]["day"] and j["best_window"].get("start") == cand[0]["start"]
                j["dataBaseline"] = cand[0]
                return j
        except Exception:
            pass  # retry, then fall back
    return fallback


# Example Flask route:
#   @app.get("/api/best-time")
#   def api_best_time():
#       rows = db_rows()  # e.g. [dict(r) for r in cursor.execute("SELECT latitude, longitude, date, time, vehicle_count FROM traffic")]
#       return best_time(rows, float(request.args["lat"]), float(request.args["lon"]), int(request.args.get("hours", 4)))