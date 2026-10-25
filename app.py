
import asyncio
import json
import os
from datetime import date, timedelta
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import altair as alt
import asyncpg
import folium
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

load_dotenv()

st.set_page_config(
    page_title="RoadworkWise",
    page_icon="🚧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] {display:none;}
    .block-container {
        padding-top: 1rem;
        max-width: 1500px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("RoadworkWise")
st.caption(
    "Forecast traffic demand, check weather conditions "
    "and find a more efficient time for roadworks."
)


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

async def query_database(sql, *args):
    conn = await asyncio.wait_for(
        asyncpg.connect(
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", "30650")),
            database=os.getenv("DB_NAME", "tsdb"),
            ssl=os.getenv("DB_SSL", "require"),
            timeout=20,
            command_timeout=120,
        ),
        timeout=25,
    )

    try:
        return await conn.fetch(sql, *args, timeout=120)
    finally:
        await conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def load_sensors():
    rows = asyncio.run(query_database("""
        SELECT
            id AS sensor_id,
            name AS "Name",
            district,
            latitude AS "Latitude",
            longitude AS "Longitude"
        FROM public.detectors
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
        ORDER BY id
    """))

    return pd.DataFrame(
        [dict(row) for row in rows],
        columns=[
            "sensor_id",
            "Name",
            "district",
            "Latitude",
            "Longitude",
        ],
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_traffic():
    rows = asyncio.run(query_database("""
        SELECT
            date AS "Hour",
            id AS sensor_id,
            vehicles AS "Vehicle Count"
        FROM public.traffic
        WHERE date IS NOT NULL
          AND vehicles IS NOT NULL
        ORDER BY id, date
    """))

    df = pd.DataFrame(
        [dict(row) for row in rows],
        columns=["Hour", "sensor_id", "Vehicle Count"],
    )

    if not df.empty:
        df["Hour"] = pd.to_datetime(
            df["Hour"], errors="coerce"
        )
        df["Vehicle Count"] = pd.to_numeric(
            df["Vehicle Count"], errors="coerce"
        )
        df["sensor_id"] = pd.to_numeric(
            df["sensor_id"], errors="coerce"
        )

        df = df.dropna(
            subset=["Hour", "Vehicle Count", "sensor_id"]
        ).copy()

        df["sensor_id"] = df["sensor_id"].astype(int)

    return df


try:
    with st.spinner(
        "Loading camera locations and historical traffic data..."
    ):
        sensors = load_sensors()
        traffic = load_traffic()

except Exception as exc:
    st.error("Could not load data from the traffic database.")

    with st.expander("Technical details"):
        st.code(str(exc))

    st.stop()


if sensors.empty:
    st.error("No cameras with valid coordinates were found.")
    st.stop()

sensors["sensor_id"] = pd.to_numeric(
    sensors["sensor_id"], errors="coerce"
)
sensors["Latitude"] = pd.to_numeric(
    sensors["Latitude"], errors="coerce"
)
sensors["Longitude"] = pd.to_numeric(
    sensors["Longitude"], errors="coerce"
)

sensors = sensors.dropna(
    subset=["sensor_id", "Latitude", "Longitude"]
).copy()

sensors["sensor_id"] = sensors["sensor_id"].astype(int)

if traffic.empty:
    st.warning("No historical traffic readings were returned.")
    st.stop()


# --------------------------------------------------
# CAMERA MAP
# --------------------------------------------------

st.subheader("Traffic camera map")
st.write("Click a camera marker on the map to select it.")

traffic_map = folium.Map(
    location=[
        float(sensors["Latitude"].mean()),
        float(sensors["Longitude"].mean()),
    ],
    zoom_start=11,
    tiles="OpenStreetMap",
    control_scale=True,
)

cluster = MarkerCluster(
    name="Traffic cameras",
    options={"showCoverageOnHover": False},
).add_to(traffic_map)

for _, row in sensors.iterrows():
    sid = int(row["sensor_id"])

    name = str(row["Name"] or f"Camera {sid}")
    district = str(row["district"] or "Unknown")

    folium.Marker(
        location=[
            float(row["Latitude"]),
            float(row["Longitude"]),
        ],
        tooltip=f"Camera ID: {sid}",
        popup=folium.Popup(
            f"<b>{name}</b><br>"
            f"Camera ID: {sid}<br>"
            f"District: {district}",
            max_width=280,
        ),
        icon=folium.Icon(
            color="blue",
            icon="camera",
            prefix="fa",
        ),
    ).add_to(cluster)

map_result = st_folium(
    traffic_map,
    height=580,
    use_container_width=True,
    returned_objects=["last_object_clicked_tooltip"],
    key="roadworkwise_map",
)

if "selected_camera" not in st.session_state:
    st.session_state["selected_camera"] = None

tooltip = (map_result or {}).get(
    "last_object_clicked_tooltip"
)

if tooltip and "Camera ID:" in str(tooltip):
    try:
        clicked_id = int(
            str(tooltip).split("Camera ID:")[-1].strip()
        )

        if clicked_id in set(sensors["sensor_id"]):
            st.session_state["selected_camera"] = clicked_id

    except (ValueError, TypeError):
        pass

selected_id = st.session_state["selected_camera"]

if selected_id is None:
    st.info("Click a camera marker on the map to get started.")
    st.stop()


# --------------------------------------------------
# SELECTED CAMERA
# --------------------------------------------------

camera_matches = sensors.loc[
    sensors["sensor_id"] == selected_id
]

if camera_matches.empty:
    st.warning("The selected camera could not be found.")
    st.stop()

camera = camera_matches.iloc[0]

camera_data = traffic.loc[
    traffic["sensor_id"] == selected_id
].copy()

st.divider()

st.subheader(
    f"📍 {camera['Name'] or f'Camera {selected_id}'}"
)

st.caption(
    f"Camera ID: {selected_id} | "
    f"District: {camera['district'] or 'Unknown'} | "
    f"Latitude: {float(camera['Latitude']):.6f} | "
    f"Longitude: {float(camera['Longitude']):.6f}"
)

if camera_data.empty:
    st.warning(
        "No historical readings were found for this camera."
    )
    st.stop()

camera_data["minute"] = (
    camera_data["Hour"].dt.hour * 60
    + camera_data["Hour"].dt.minute
)

camera_data["slot"] = (
    camera_data["minute"] // 15
) * 15


# --------------------------------------------------
# HISTORICAL TRAFFIC GRAPH
# --------------------------------------------------

daily = (
    camera_data.groupby("slot", as_index=False)
    .agg(
        average_vehicles=("Vehicle Count", "mean"),
        reading_count=("Vehicle Count", "count"),
        distinct_days=(
            "Hour",
            lambda values: values.dt.date.nunique(),
        ),
    )
)

all_slots = pd.DataFrame({
    "slot": list(range(0, 1440, 15))
})

daily = all_slots.merge(
    daily,
    on="slot",
    how="left",
)

daily["time"] = daily["slot"].apply(
    lambda value: (
        f"{int(value // 60):02d}:"
        f"{int(value % 60):02d}"
    )
)

st.subheader("Historical traffic patterns")

st.write(
    "Explore the average recorded vehicle count for each "
    "15-minute period across the available historical data."
)

chart = (
    alt.Chart(daily)
    .mark_line(
        point=True,
        color="#2878B5",
        strokeWidth=3,
    )
    .encode(
        x=alt.X(
            "slot:Q",
            title="Time of day",
            scale=alt.Scale(
                domain=[0, 1440],
                nice=False,
            ),
            axis=alt.Axis(
                values=list(range(0, 1441, 120)),
                labelExpr=(
                    "floor(datum.value / 60) < 10 "
                    "? '0' + floor(datum.value / 60) + ':00' "
                    ": floor(datum.value / 60) + ':00'"
                ),
            ),
        ),
        y=alt.Y(
            "average_vehicles:Q",
            title="Average recorded vehicle count",
            scale=alt.Scale(zero=True),
        ),
        tooltip=[
            alt.Tooltip("time:N", title="Time"),
            alt.Tooltip(
                "average_vehicles:Q",
                title="Average vehicles",
                format=".2f",
            ),
            alt.Tooltip(
                "reading_count:Q",
                title="Readings",
            ),
            alt.Tooltip(
                "distinct_days:Q",
                title="Days represented",
            ),
        ],
    )
    .properties(height=350)
    .interactive()
)

st.altair_chart(chart, use_container_width=True)


# --------------------------------------------------
# WEATHER API
# --------------------------------------------------

def fetch_weather_forecast(latitude, longitude, planned_date):
    """
    Retrieve hourly weather data from Open-Meteo.

    Forecast availability depends on the selected date.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": (
            "temperature_2m,"
            "precipitation_probability,"
            "precipitation,"
            "wind_speed_10m"
        ),
        "timezone": "auto",
        "start_date": planned_date.isoformat(),
        "end_date": planned_date.isoformat(),
    }

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urlencode(params)
    )

    request = Request(
        url,
        headers={"User-Agent": "RoadworkWise/1.0"},
    )

    with urlopen(request, timeout=15) as response:
        payload = json.loads(
            response.read().decode("utf-8")
        )

    hourly = payload.get("hourly")

    if not hourly or not hourly.get("time"):
        raise ValueError(
            "The weather API returned no hourly forecast "
            "for this date."
        )

    weather = pd.DataFrame({
        "time": pd.to_datetime(hourly["time"]),
        "temperature_2m": hourly.get("temperature_2m"),
        "precipitation_probability": hourly.get(
            "precipitation_probability"
        ),
        "precipitation": hourly.get("precipitation"),
        "wind_speed_10m": hourly.get("wind_speed_10m"),
    })

    return weather


def weather_for_window(weather, start_minute, end_minute):
    """
    Average hourly weather values overlapping a work window.
    Weather is hourly, so this is an approximation.
    """
    if weather is None or weather.empty:
        return None

    weather = weather.copy()

    weather["minute"] = (
        weather["time"].dt.hour * 60
        + weather["time"].dt.minute
    )

    selected = weather[
        (weather["minute"] >= start_minute)
        & (weather["minute"] < end_minute)
    ]

    if selected.empty:
        midpoint = (start_minute + end_minute) / 2

        weather["distance"] = (
            weather["minute"] - midpoint
        ).abs()

        selected = weather.nsmallest(1, "distance")

    result = {}

    for column, output_name in [
        ("temperature_2m", "temperature"),
        ("precipitation_probability", "rain_probability"),
        ("precipitation", "precipitation"),
        ("wind_speed_10m", "wind_speed"),
    ]:
        if column in selected.columns:
            values = pd.to_numeric(
                selected[column],
                errors="coerce",
            ).dropna()

            result[output_name] = (
                float(values.mean())
                if not values.empty
                else None
            )

    return result


def weather_risk(summary):
    """
    A transparent heuristic indicator, not a safety prediction.
    Higher values indicate less favourable weather conditions.
    """
    if not summary:
        return None

    score = 0.0
    used = False

    rain_probability = summary.get("rain_probability")
    precipitation = summary.get("precipitation")
    wind_speed = summary.get("wind_speed")

    if rain_probability is not None:
        score += 0.5 * max(
            0,
            min(100, rain_probability),
        )
        used = True

    if precipitation is not None:
        score += 10 * max(
            0,
            min(3, precipitation),
        )
        used = True

    if wind_speed is not None:
        score += 0.5 * max(
            0,
            min(40, wind_speed),
        )
        used = True

    return min(100.0, score) if used else None


def weather_description(summary):
    if not summary:
        return "Weather unavailable"

    bits = []

    if summary.get("temperature") is not None:
        bits.append(
            f"{summary['temperature']:.0f}°C"
        )

    if summary.get("rain_probability") is not None:
        bits.append(
            f"{summary['rain_probability']:.0f}% rain chance"
        )

    if summary.get("precipitation") is not None:
        bits.append(
            f"{summary['precipitation']:.1f} mm rain"
        )

    if summary.get("wind_speed") is not None:
        bits.append(
            f"wind {summary['wind_speed']:.0f} km/h"
        )

    return (
        " · ".join(bits)
        if bits
        else "Weather details unavailable"
    )


# --------------------------------------------------
# ROADWORKS PLANNING FORM
# --------------------------------------------------

st.divider()

st.subheader("Plan your roadworks")

st.write(
    "Choose a date, duration and permitted working hours. "
    "RoadworkWise estimates traffic demand from historical "
    "patterns and checks the weather forecast before ranking "
    "possible work windows."
)

with st.form("roadworks_schedule"):
    planned_date = st.date_input(
        "Planned roadworks date",
        value=date.today() + timedelta(days=1),
        min_value=date.today(),
    )

    duration_hours = st.selectbox(
        "Roadworks duration",
        options=[0.5, 1, 1.5, 2, 3, 4, 6, 8, 12],
        index=3,
        format_func=lambda value: (
            f"{value:g} hour"
            if value == 1
            else f"{value:g} hours"
        ),
    )

    col1, col2 = st.columns(2)

    with col1:
        earliest = st.time_input(
            "Earliest start time",
            value=pd.Timestamp("06:00").time(),
            step=900,
        )

    with col2:
        latest_finish = st.time_input(
            "Latest finish time",
            value=pd.Timestamp("22:00").time(),
            step=900,
        )

    day_choice = st.selectbox(
        "Day of week",
        [
            "Use selected date",
            "Any matching weekday",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
    )

    find_best = st.form_submit_button(
        "Forecast demand and find the best time",
        type="primary",
        use_container_width=True,
    )


def minute_of_day(value):
    return value.hour * 60 + value.minute


def format_time(value):
    return (
        f"{int(value // 60):02d}:"
        f"{int(value % 60):02d}"
    )


# --------------------------------------------------
# FORECAST AND RANK RECOMMENDATIONS
# --------------------------------------------------

if find_best:
    earliest_min = minute_of_day(earliest)
    finish_min = minute_of_day(latest_finish)
    duration_min = int(duration_hours * 60)

    if finish_min <= earliest_min:
        st.error(
            "The finish time must be later than "
            "the earliest start time."
        )
        st.stop()

    if earliest_min + duration_min > finish_min:
        st.error(
            "The roadworks duration does not fit "
            "in the permitted time range."
        )
        st.stop()

    if planned_date < date.today():
        st.error("Please select today or a future date.")
        st.stop()

    # Select historical data matching the target weekday.
    if day_choice == "Use selected date":
        target_weekday = planned_date.weekday()

    elif day_choice == "Any matching weekday":
        target_weekday = None

    else:
        target_weekday = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ].index(day_choice)

    data = camera_data.copy()

    if target_weekday is not None:
        data = data[
            data["Hour"].dt.dayofweek == target_weekday
        ].copy()

    if data.empty:
        st.warning(
            "There are no historical readings for the "
            "selected weekday. Try another date or weekday."
        )
        st.stop()

    data["date_only"] = data["Hour"].dt.date

    data["minute"] = (
        data["Hour"].dt.hour * 60
        + data["Hour"].dt.minute
    )

    data["slot"] = (
        data["minute"] // 15
    ) * 15

    # Average each day's readings first, then average across days.
    by_day = (
        data.groupby(
            ["date_only", "slot"],
            as_index=False,
        )
        .agg(
            vehicles=("Vehicle Count", "mean")
        )
    )

    profile = (
        by_day.groupby("slot")["vehicles"]
        .mean()
    )

    sample_days = int(
        data["date_only"].nunique()
    )

    # Fetch weather for the selected camera coordinates.
    weather = None
    weather_error = None

    with st.spinner(
        "Fetching the hourly weather forecast..."
    ):
        try:
            weather = fetch_weather_forecast(
                float(camera["Latitude"]),
                float(camera["Longitude"]),
                planned_date,
            )

        except (
            URLError,
            HTTPError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            weather_error = str(exc)

        except Exception as exc:
            weather_error = str(exc)

    candidates = []
    last_start = finish_min - duration_min

    for start in range(
        earliest_min,
        last_start + 1,
        15,
    ):
        end = start + duration_min

        first_slot = (start // 15) * 15
        last_slot = ((end - 1) // 15) * 15

        slots = list(
            range(
                first_slot,
                last_slot + 1,
                15,
            )
        )

        if not slots:
            continue

        # Do not silently invent traffic for missing slots.
        if not all(slot in profile.index for slot in slots):
            continue

        values = profile.loc[slots]

        # Mean recorded count per 15-minute slot.
        demand = float(values.mean())

        wx = weather_for_window(
            weather,
            start,
            end,
        )

        wx_risk = weather_risk(wx)

        candidates.append({
            "start": start,
            "end": end,
            "average_vehicles": demand,
            "slots_covered": len(values),
            "weather": wx,
            "weather_risk": wx_risk,
        })

    if not candidates:
        st.warning(
            "There is not enough historical coverage for "
            "this duration and time range. Try a shorter "
            "duration or a wider time range."
        )
        st.stop()

    # --------------------------------------------------
    # RANK CANDIDATE WINDOWS
    # --------------------------------------------------

    demands = [
        item["average_vehicles"]
        for item in candidates
    ]

    low_demand = min(demands)
    high_demand = max(demands)

    for item in candidates:
        if high_demand > low_demand:
            item["demand_score"] = (
                100
                * (
                    item["average_vehicles"]
                    - low_demand
                )
                / (
                    high_demand
                    - low_demand
                )
            )
        else:
            item["demand_score"] = 0.0

        # Traffic demand is the main factor.
        # Weather is a secondary heuristic where available.
        if item["weather_risk"] is not None:
            item["combined_score"] = (
                0.8 * item["demand_score"]
                + 0.2 * item["weather_risk"]
            )
        else:
            item["combined_score"] = item["demand_score"]

    ranked = sorted(
        candidates,
        key=lambda item: item["combined_score"],
    )

    # Choose up to three non-overlapping alternatives.
    recommendations = []

    for item in ranked:
        overlaps = any(
            item["start"] < other["end"]
            and item["end"] > other["start"]
            for other in recommendations
        )

        if not overlaps:
            recommendations.append(item)

        if len(recommendations) == 3:
            break

    # --------------------------------------------------
    # SHOW RECOMMENDATIONS
    # --------------------------------------------------

    st.divider()
    st.subheader("Recommended roadworks times")

    if day_choice == "Any matching weekday":
        date_description = (
            "matching weekdays in the historical data"
        )
    else:
        date_description = planned_date.strftime(
            "%A %d %B %Y"
        )

    st.caption(
        f"Demand estimate for {date_description}, based "
        f"on historical readings from {sample_days} "
        f"distinct day(s)."
    )

    if weather is not None:
        st.success(
            "Recommendations combine estimated traffic "
            "demand (80%) and a weather planning indicator (20%)."
        )

        st.caption(
            "The weather weighting is a configurable heuristic, "
            "not a validated prediction of roadwork safety "
            "or traffic impact."
        )

        st.caption(
            "Weather source: Open-Meteo hourly forecast."
        )

    else:
        st.warning(
            "Weather data could not be retrieved. "
            "Recommendations use historical demand only."
        )

        if weather_error:
            with st.expander("Weather API details"):
                st.code(weather_error)

    # --------------------------------------------------
    # TOP THREE OPTIONS
    # --------------------------------------------------

    for rank, item in enumerate(
        recommendations,
        start=1,
    ):
        with st.container(border=True):
            left, right = st.columns([3, 1])

            left.markdown(
                f"### {rank}. "
                f"{format_time(item['start'])}–"
                f"{format_time(item['end'])}"
            )

            left.caption(
                "Estimated average recorded vehicle count "
                "per 15-minute slot"
            )

            right.metric(
                "Demand estimate",
                f"{item['average_vehicles']:.1f}",
            )

            if item["weather"]:
                st.write(
                    "**Forecast conditions:** "
                    + weather_description(item["weather"])
                )

                if item["weather_risk"] is not None:
                    st.caption(
                        "Weather planning indicator: "
                        f"{item['weather_risk']:.0f}/100 "
                        "(lower is more favourable)."
                    )

            if rank == 1:
                st.info(
                    "Best-ranked option under the current "
                    "demand and weather scoring assumptions. "
                    "Check local restrictions and work-specific "
                    "weather requirements before confirming."
                )


# --------------------------------------------------
# DISCLAIMER
# --------------------------------------------------

st.divider()

st.caption(
    "Traffic demand is estimated from historical readings "
    "for matching weekdays. This is a baseline estimate, "
    "not a trained machine-learning forecast or live traffic "
    "prediction. Weather is retrieved from Open-Meteo and "
    "used as a planning heuristic, not as a validated measure "
    "of safety or delay. Recommendations do not measure queue "
    "length or actual delays. Check road restrictions and "
    "work-specific weather requirements before scheduling."
)
