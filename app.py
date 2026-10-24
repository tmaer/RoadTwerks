
import asyncio
import os
from datetime import time

import asyncpg
import folium
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# --------------------------------------------------
# PAGE SETUP
# --------------------------------------------------

load_dotenv()

st.set_page_config(
    page_title="RoadworkWise",
    page_icon="🚧",
    layout="wide",
)

st.title("🚧 RoadworkWise")
st.caption(
    "Find quieter times to carry out roadworks using historical "
    "traffic sensor data."
)

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

async def query_database(sql):
    conn = await asyncio.wait_for(
        asyncpg.connect(
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", "30650")),
            database=os.getenv("DB_NAME", "tsdb"),
            ssl=os.getenv("DB_SSL", "require"),
            timeout=20,
            command_timeout=60,
        ),
        timeout=25,
    )

    try:
        return await conn.fetch(sql, timeout=60)
    finally:
        await conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def load_sensors():
    rows = asyncio.run(
        query_database("""
            SELECT
                id AS sensor_id,
                name AS sensor_name,
                district,
                latitude,
                longitude
            FROM public.detectors
            WHERE latitude IS NOT NULL
              AND longitude IS NOT NULL
            ORDER BY id
        """)
    )
    return pd.DataFrame([dict(row) for row in rows])


@st.cache_data(ttl=300, show_spinner=False)
def load_traffic():
    rows = asyncio.run(
        query_database("""
            SELECT
                date AS reading_time,
                id AS sensor_id,
                vehicles,
                intensity,
                occupancy,
                load AS traffic_load
            FROM public.traffic
            WHERE date IS NOT NULL
              AND vehicles IS NOT NULL
            ORDER BY date DESC
            LIMIT 100000
        """)
    )

    df = pd.DataFrame([dict(row) for row in rows])

    if not df.empty:
        df["reading_time"] = pd.to_datetime(
            df["reading_time"], errors="coerce"
        )
        df["vehicles"] = pd.to_numeric(
            df["vehicles"], errors="coerce"
        )
        df = df.dropna(
            subset=["reading_time", "sensor_id", "vehicles"]
        )

    return df


# --------------------------------------------------
# LOAD DATA
# --------------------------------------------------

with st.sidebar:
    st.header("Settings")
    st.write("Data source: TigerData")

    if st.button("Refresh database data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

try:
    with st.spinner("Loading cameras and traffic readings..."):
        sensors = load_sensors()
        traffic = load_traffic()

except Exception as exc:
    st.error("Could not load data from TigerData.")
    st.write(
        "Check that your .env file is in the same folder as app.py "
        "and that your database credentials are correct."
    )
    with st.expander("Technical details"):
        st.code(str(exc))
    st.stop()


if sensors.empty:
    st.error("No cameras with valid coordinates were found.")
    st.stop()

sensors["sensor_id"] = pd.to_numeric(
    sensors["sensor_id"], errors="coerce"
)
traffic["sensor_id"] = pd.to_numeric(
    traffic["sensor_id"], errors="coerce"
)

sensors = sensors.dropna(
    subset=["sensor_id", "latitude", "longitude"]
).copy()

sensors["sensor_id"] = sensors["sensor_id"].astype(int)
traffic = traffic.dropna(subset=["sensor_id"]).copy()
traffic["sensor_id"] = traffic["sensor_id"].astype(int)

# Calculate each camera's latest reading once, not inside the map loop.
if not traffic.empty:
    latest_by_sensor = (
        traffic.sort_values("reading_time")
        .drop_duplicates("sensor_id", keep="last")
        .set_index("sensor_id")
    )
else:
    latest_by_sensor = pd.DataFrame()

sensor_ids = set(sensors["sensor_id"].tolist())
traffic_ids = set(traffic["sensor_id"].tolist())

# --------------------------------------------------
# SMALL STATUS LINE
# --------------------------------------------------

m1, m2, m3 = st.columns(3)

m1.metric("Available cameras", f"{len(sensors):,}")
m2.metric("Loaded traffic readings", f"{len(traffic):,}")

if not traffic.empty:
    latest_timestamp = traffic["reading_time"].max()
    m3.metric(
        "Latest timestamp in loaded data",
        latest_timestamp.strftime("%d %b %Y, %H:%M"),
    )
else:
    m3.metric("Traffic readings", "None found")

# --------------------------------------------------
# INTERACTIVE MAP
# --------------------------------------------------

st.subheader("1. Select a traffic camera")
st.write(
    "Click a camera marker on the map, or select a camera using "
    "the searchable dropdown below."
)

map_center = [
    float(sensors["latitude"].mean()),
    float(sensors["longitude"].mean()),
]

traffic_map = folium.Map(
    location=map_center,
    zoom_start=11,
    tiles="OpenStreetMap",
    control_scale=True,
)

cluster = MarkerCluster(
    name="Traffic cameras",
    options={"showCoverageOnHover": False},
).add_to(traffic_map)

for row in sensors.itertuples(index=False):
    sid = int(row.sensor_id)
    name = str(row.sensor_name)
    district = str(row.district or "Unknown")

    if sid in latest_by_sensor.index:
        reading = latest_by_sensor.loc[sid]
        latest_count = reading["vehicles"]
        latest_time = reading["reading_time"]

        count_text = (
            f"{latest_count:.0f}"
            if pd.notna(latest_count)
            else "Unavailable"
        )
        time_text = latest_time.strftime("%d %b %Y %H:%M")
        marker_colour = "green"
    else:
        count_text = "Unavailable"
        time_text = "No loaded reading"
        marker_colour = "gray"

    popup_html = (
        f"<b>{name}</b><br>"
        f"Camera ID: {sid}<br>"
        f"District: {district}<br>"
        f"Latitude: {float(row.latitude):.6f}<br>"
        f"Longitude: {float(row.longitude):.6f}<br>"
        f"Latest recorded vehicle count: {count_text}<br>"
        f"Reading time: {time_text}"
    )

    folium.Marker(
        location=[float(row.latitude), float(row.longitude)],
        tooltip=f"Camera ID: {sid}",
        popup=folium.Popup(popup_html, max_width=320),
        icon=folium.Icon(
            color=marker_colour,
            icon="camera",
            prefix="fa",
        ),
    ).add_to(cluster)

map_result = st_folium(
    traffic_map,
    height=560,
    use_container_width=True,
    returned_objects=["last_object_clicked_tooltip"],
    key="roadworkwise_main_map",
)

# Keep the selected camera after Streamlit reruns.
if "selected_sensor_id" not in st.session_state:
    st.session_state["selected_sensor_id"] = int(
        sensors.iloc[0]["sensor_id"]
    )

tooltip = (map_result or {}).get("last_object_clicked_tooltip")

if tooltip:
    try:
        clicked_id = int(tooltip.split("Camera ID:")[-1].strip())
        if clicked_id in sensor_ids:
            st.session_state["selected_sensor_id"] = clicked_id
    except (ValueError, TypeError):
        pass

sensor_labels = {
    int(row.sensor_id): (
        f"{row.sensor_name} | ID {int(row.sensor_id)} "
        f"| {row.district or 'Unknown district'}"
    )
    for row in sensors.itertuples(index=False)
}

current_id = st.session_state["selected_sensor_id"]
if current_id not in sensor_labels:
    current_id = int(sensors.iloc[0]["sensor_id"])

selected_id = st.selectbox(
    "Search or choose a camera",
    options=list(sensor_labels.keys()),
    index=list(sensor_labels.keys()).index(current_id),
    format_func=lambda sid: sensor_labels[sid],
    key="camera_dropdown",
)

st.session_state["selected_sensor_id"] = selected_id

selected_sensor = sensors.loc[
    sensors["sensor_id"] == selected_id
].iloc[0]

sensor_traffic = traffic.loc[
    traffic["sensor_id"] == selected_id
].copy()

# --------------------------------------------------
# SELECTED CAMERA DETAILS
# --------------------------------------------------

st.divider()
st.subheader("2. Camera details")

d1, d2, d3 = st.columns(3)

d1.metric("Camera", str(selected_sensor["sensor_name"]))
d2.metric("Camera ID", str(selected_id))
d3.metric(
    "District",
    str(selected_sensor["district"] or "Unknown"),
)

coord1, coord2 = st.columns(2)
coord1.write(f"**Latitude:** {selected_sensor['latitude']}")
coord2.write(f"**Longitude:** {selected_sensor['longitude']}")

if not sensor_traffic.empty:
    sensor_traffic = sensor_traffic.sort_values("reading_time")
    last_reading = sensor_traffic.iloc[-1]

    r1, r2 = st.columns(2)
    r1.metric(
        "Latest recorded vehicle count",
        f"{last_reading['vehicles']:.0f}",
    )
    r2.metric(
        "Reading timestamp",
        last_reading["reading_time"].strftime("%d %b %Y, %H:%M"),
    )

    with st.expander("View historical traffic at this camera"):
        chart_data = sensor_traffic.set_index("reading_time")[
            ["vehicles"]
        ].rename(columns={"vehicles": "Recorded vehicles"})
        st.line_chart(chart_data)
        st.caption(
            "The chart shows recorded vehicle counts, not live traffic."
        )
else:
    st.warning(
        "No traffic readings for this camera were found in the "
        "100,000 readings loaded by this app. Try another camera."
    )

# --------------------------------------------------
# ROADWORKS SCHEDULER
# --------------------------------------------------

st.divider()
st.subheader("3. Plan your roadworks")

st.write(
    "Choose how long the work will take and the time range in which "
    "it is permitted to start and finish."
)

with st.form("roadworks_form"):
    duration_hours = st.selectbox(
        "Roadworks duration",
        options=[0.5, 1, 1.5, 2, 3, 4, 6, 8, 12],
        index=3,
        format_func=lambda x: (
            f"{x:g} hour" if x == 1 else f"{x:g} hours"
        ),
    )

    start_col, end_col = st.columns(2)

    with start_col:
        earliest_start = st.time_input(
            "Earliest permitted start",
            value=time(6, 0),
            step=900,
        )

    with end_col:
        latest_finish = st.time_input(
            "Latest permitted finish",
            value=time(22, 0),
            step=900,
        )

    selected_weekday = st.selectbox(
        "Day of week",
        options=[
            "Any day",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
    )

    submitted = st.form_submit_button(
        "Find the best time",
        type="primary",
        use_container_width=True,
    )

# --------------------------------------------------
# RECOMMENDATION ENGINE
# --------------------------------------------------

def minutes_since_midnight(value):
    return value.hour * 60 + value.minute


def calculate_recommendations(
    df,
    duration_hours,
    earliest,
    latest,
    weekday,
):
    """
    Score candidate windows using historical average vehicle counts.
    Each historical date contributes its average count for the window.
    """
    if df.empty:
        return pd.DataFrame(), "No historical readings are available."

    duration_minutes = int(round(duration_hours * 60))
    earliest_minutes = minutes_since_midnight(earliest)
    latest_minutes = minutes_since_midnight(latest)

    if latest_minutes <= earliest_minutes:
        return (
            pd.DataFrame(),
            "The permitted finish time must be later than the "
            "earliest start time. Overnight scheduling is not "
            "supported in this version.",
        )

    if earliest_minutes + duration_minutes > latest_minutes:
        return (
            pd.DataFrame(),
            "The roadworks duration does not fit inside the permitted "
            "time range.",
        )

    work = df.copy()
    work["date_only"] = work["reading_time"].dt.date
    work["minute_of_day"] = (
        work["reading_time"].dt.hour * 60
        + work["reading_time"].dt.minute
    )

    if weekday != "Any day":
        weekday_number = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ].index(weekday)

        work = work[
            work["reading_time"].dt.dayofweek == weekday_number
        ]

    if work.empty:
        return (
            pd.DataFrame(),
            "There are no loaded readings for the selected day. "
            "Try Any day or select another camera.",
        )

    # Score starts every 15 minutes.
    results = []

    last_start = latest_minutes - duration_minutes

    for start_minute in range(
        earliest_minutes,
        last_start + 1,
        15,
    ):
        end_minute = start_minute + duration_minutes

        window_rows = work[
            (work["minute_of_day"] >= start_minute)
            & (work["minute_of_day"] < end_minute)
        ]

        if window_rows.empty:
            continue

        # Average within each date first, then across dates.
        per_day = window_rows.groupby("date_only")["vehicles"].mean()
        if per_day.empty:
            continue

        results.append({
            "start": start_minute,
            "end": end_minute,
            "average_vehicles": float(per_day.mean()),
            "days_sampled": int(per_day.count()),
            "sample_readings": int(len(window_rows)),
        })

    if not results:
        return (
            pd.DataFrame(),
            "There is not enough historical data to score suitable "
            "windows for these settings.",
        )

    ranked = pd.DataFrame(results).sort_values(
        "average_vehicles"
    )

    # Choose up to three windows that do not overlap.
    chosen = []

    for candidate in ranked.to_dict("records"):
        overlaps = any(
            candidate["start"] < existing["end"]
            and candidate["end"] > existing["start"]
            for existing in chosen
        )

        if not overlaps:
            chosen.append(candidate)

        if len(chosen) == 3:
            break

    return pd.DataFrame(chosen), None


def format_clock(minutes):
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


if submitted:
    recommendations, error = calculate_recommendations(
        sensor_traffic,
        duration_hours,
        earliest_start,
        latest_finish,
        selected_weekday,
    )

    st.divider()
    st.subheader("4. Recommended roadworks windows")

    if error:
        st.warning(error)

    elif recommendations.empty:
        st.warning("No suitable windows could be calculated.")

    else:
        st.success(
            "Ranked from lowest to highest historical average "
            "vehicle count among the available time windows."
        )

        for rank, result in enumerate(
            recommendations.to_dict("records"),
            start=1,
        ):
            with st.container(border=True):
                left, right = st.columns([3, 1])

                with left:
                    st.markdown(f"### {rank}. {format_clock(result['start'])}–{format_clock(result['end'])}")
                    st.write(
                        f"Historical average: "
                        f"**{result['average_vehicles']:.1f} vehicles "
                        f"per recorded interval**"
                    )
                    st.caption(
                        f"Based on {result['days_sampled']} historical "
                        f"date(s) and {result['sample_readings']} "
                        f"readings in this window."
                    )

                with right:
                    if rank == 1:
                        st.markdown("**Best-ranked option**")
                    else:
                        st.markdown(f"Alternative {rank - 1}")

        st.info(
            "These recommendations use historical readings loaded by "
            "the app. They are estimates of traffic volume, not a "
            "prediction of live congestion, queues, or roadworks delays."
        )

        export_df = recommendations.copy()
        export_df["start_time"] = export_df["start"].apply(format_clock)
        export_df["finish_time"] = export_df["end"].apply(format_clock)
        export_df["camera_id"] = selected_id
        export_df["camera_name"] = str(selected_sensor["sensor_name"])
        export_df["latitude"] = float(selected_sensor["latitude"])
        export_df["longitude"] = float(selected_sensor["longitude"])
        export_df["duration_hours"] = duration_hours

        export_columns = [
            "camera_name",
            "camera_id",
            "latitude",
            "longitude",
            "duration_hours",
            "start_time",
            "finish_time",
            "average_vehicles",
            "days_sampled",
            "sample_readings",
        ]

        st.download_button(
            "Download recommendations as CSV",
            data=export_df[export_columns].to_csv(index=False),
            file_name="roadworkwise_recommendations.csv",
            mime="text/csv",
            use_container_width=True,
        )

# --------------------------------------------------
# FOOTER
# --------------------------------------------------

st.divider()
st.caption(
    "RoadworkWise is a planning aid. Check local restrictions, "
    "permits, safety requirements and current road conditions before "
    "scheduling roadworks."
)
