"""
RoadworkWise — calculation engine.

For a road section and a planned roadwork (lanes closed, duration), it computes
for every possible start hour:
  - congestion level before and during the works (volume / capacity)
  - extra travel minutes per vehicle
  - total hours lost by all drivers
and recommends the least disruptive time slot.

Method: BPR function (Bureau of Public Roads), the standard transport
engineering formula for travel time as a function of road saturation:
    t = t0 * (1 + 0.15 * (V/C)^4)
Closing lanes reduces capacity C, so V/C rises and so does travel time.

Input data: avenue_traffic.csv (output of 1_extract_avenue.py).
"""
import pandas as pd

# --- Assumptions (tune them and mention them in the pitch) ---
CAPACITY_PER_LANE = 1800   # vehicles/hour per lane (urban arterial)
FREE_FLOW_KMH = 50         # speed with no traffic
ALPHA, BETA = 0.15, 4      # standard BPR parameters


def volume_by_hour(csv, detector_ids, weekday):
    """Mean volume (veh/h) for each hour on a given weekday (0 = Monday)."""
    df = pd.read_csv(csv, parse_dates=["date"])
    df = df[df["id"].isin(detector_ids) & (df["date"].dt.day_of_week == weekday)]
    # average across the section's detectors, per hour
    per_det = df.groupby([df["date"].dt.hour.rename("hour"), "id"])["intensity"].mean()
    return per_det.groupby("hour").mean().reindex(range(24), fill_value=0)


def travel_minutes(volume, lanes, length_km):
    t0 = length_km / FREE_FLOW_KMH * 60
    vc = volume / (lanes * CAPACITY_PER_LANE)
    return t0 * (1 + ALPHA * vc ** BETA), vc


def evaluate_roadwork(vol_by_hour, total_lanes, closed_lanes, duration_h, length_km):
    rows = []
    open_lanes = total_lanes - closed_lanes
    for start in range(0, 24 - duration_h + 1):
        hours = range(start, start + duration_h)
        max_extra, lost, vc_normal, vc_works = 0, 0, 0, 0
        for h in hours:
            v = vol_by_hour[h]
            t_n, vc_n = travel_minutes(v, total_lanes, length_km)
            t_w, vc_w = travel_minutes(v, open_lanes, length_km)
            extra = t_w - t_n
            max_extra = max(max_extra, extra)
            lost += extra * v / 60                # vehicle-hours lost
            vc_normal, vc_works = max(vc_normal, vc_n), max(vc_works, vc_w)
        rows.append({
            "start": f"{start:02d}:00",
            "extra_min_per_trip": round(max_extra, 1),
            "total_hours_lost": round(lost),
            "normal_congestion_%": round(vc_normal * 100),
            "works_congestion_%": round(vc_works * 100),
        })
    return pd.DataFrame(rows).sort_values("total_hours_lost")


if __name__ == "__main__":
    # Example: section with 2 detectors, Monday, 3 lanes, 1 closed for 3 hours
    ids = pd.read_csv("avenue_detectors.csv")["id"].head(2).tolist()
    vol = volume_by_hour("avenue_traffic.csv", ids, weekday=0)
    res = evaluate_roadwork(vol, total_lanes=3, closed_lanes=1,
                            duration_h=3, length_km=1.5)
    print(res.head(5).to_string(index=False))

    best = res.iloc[0]
    worst = res.iloc[-1]
    print(f"\nBest start: {best['start']} (+{best['extra_min_per_trip']} min per trip). "
          f"Worst: {worst['start']} (+{worst['extra_min_per_trip']} min).")
