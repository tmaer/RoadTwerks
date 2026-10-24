
import asyncio
import os

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
    page_icon="",
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
st.caption("Find quieter times for roadworks using historical traffic data.")


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
    rows = asyncio.run(query_database("""
        SELECT id AS sensor_id,
               name AS sensor_name,
               district,
               latitude,
               longitude
        FROM public.detectors
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
        ORDER BY id
    """))

    return pd.DataFrame(
        [dict(row) for row in rows],
        columns=[
            "sensor_id", "sensor_name", "district",
            "latitude", "longitude",
        ],
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_traffic():
    rows = asyncio.run(query_database("""
        SELECT date AS reading_time,
               id AS sensor_id,
               vehicles
        FROM public.traffic
        WHERE date IS NOT NULL
          AND vehicles IS NOT NULL
        ORDER BY date DESC
        LIMIT 100000
    """))

    df = pd.DataFrame(
        [dict(row) for row in rows],
        columns=["reading_time", "sensor_id", "vehicles"],
    )

    if not df.empty:
        df["reading_time"] = pd.to_datetime(
            df["reading_time"], errors="coerce"
        )
        df["vehicles"] = pd.to_numeric(
            df["vehicles"], errors="coerce"
        )
        df["sensor_id"] = pd.to_numeric(
            df["sensor_id"], errors="coerce"
        )
        df = df.dropna(
            subset=["reading_time", "vehicles", "sensor_id"]
        ).copy()
        df["sensor_id"] = df["sensor_id"].astype(int)

    return df


try:
    with st.spinner("Loading camera locations and traffic history..."):
        sensors = load_sensors()
        traffic = load_traffic()
except Exception as exc:
    st.error("Could not connect to the traffic database.")
    with st.expander("Technical details"):
        st.code(str(exc))
    st.stop()

if sensors.empty:
    st.error("No cameras with valid coordinates were found.")
    st.stop()

sensors["sensor_id"] = pd.to_numeric(
    sensors["sensor_id"], errors="coerce"
)
sensors["latitude"] = pd.to_numeric(
    sensors["latitude"], errors="coerce"
)
sensors["longitude"] = pd.to_numeric(
    sensors["longitude"], errors="coerce"
)
sensors = sensors.dropna(
    subset=["sensor_id", "latitude", "longitude"]
).copy()
sensors["sensor_id"] = sensors["sensor_id"].astype(int)

if traffic.empty:
    st.warning("No historical traffic readings were returned.")
    st.stop()


# --------------------------------------------------
# CAMERA MAP
# --------------------------------------------------

st.subheader("Traffic camera map")
st.write(
    "Click a camera marker to select it, or use the dropdown below."
)

traffic_map = folium.Map(
    location=[
        float(sensors["latitude"].mean()),
        float(sensors["longitude"].mean()),
    ],
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
    name = str(row.sensor_name or f"Camera {sid}")
    district = str(row.district or "Unknown")

    folium.Marker(
        location=[float(row.latitude), float(row.longitude)],
        tooltip=f"Camera ID: {sid}",
        popup=folium.Popup(
            f"<b>{name}</b><br>"
            f"Camera ID: {sid}<br>"
            f"District: {district}",
            max_width=280,
        ),
        icon=folium.Icon(
            color="blue", icon="camera", prefix="fa"
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

# Use the clicked marker tooltip when Streamlit returns it.
tooltip = (map_result or {}).get("last_object_clicked_tooltip")

if tooltip and "Camera ID:" in str(tooltip):
    try:
        clicked_id = int(
            str(tooltip).split("Camera ID:")[-1].strip()
        )
        if clicked_id in set(sensors["sensor_id"]):
            st.session_state["selected_camera"] = clicked_id
    except (ValueError, TypeError):
        pass

# Searchable fallback selector.
sensor_options = {}
for row in sensors.itertuples(index=False):
    sid = int(row.sensor_id)
    name = str(row.sensor_name or f"Camera {sid}")
    district = str(row.district or "Unknown")
    label = f"{name} | ID {sid} | {district}"
    sensor_options[label] = sid

labels = list(sensor_options.keys())
current_id = st.session_state["selected_camera"]

default_index = next(
    (
        i for i, label in enumerate(labels)
        if sensor_options[label] == current_id
    ),
    None,
)

chosen_label = st.selectbox(
    "Search or choose a camera",
    options=labels,
    index=default_index,
    placeholder="Type a camera name, ID or district...",
    key="camera_selector",
)

if chosen_label:
    st.session_state["selected_camera"] = sensor_options[chosen_label]

selected_id = st.session_state["selected_camera"]

if selected_id is None:
    st.info("Choose a camera above to display its traffic graph.")
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
st.subheader(f"📍 {camera['sensor_name'] or f'Camera {selected_id}'}")

st.caption(
    f"Camera ID: {selected_id}  |  "
    f"District: {camera['district'] or 'Unknown'}  |  "
    f"Latitude: {float(camera['latitude']):.6f}  |  "
    f"Longitude: {float(camera['longitude']):.6f}"
)

if camera_data.empty:
    st.warning(
        "No historical readings were found for this camera "
        "in the loaded dataset."
    )
    st.stop()

camera_data["minute"] = (
    camera_data["reading_time"].dt.hour * 60
    + camera_data["reading_time"].dt.minute
)
camera_data["slot"] = (
    camera_data["minute"] // 15
) * 15


# --------------------------------------------------
# 24-HOUR AVERAGE TRAFFIC GRAPH
# --------------------------------------------------

daily = (
    camera_data.groupby("slot", as_index=False)
    .agg(
        average_vehicles=("vehicles", "mean"),
        reading_count=("vehicles", "count"),
        distinct_days=(
            "reading_time",
            lambda values: values.dt.date.nunique(),
        ),
    )
)

daily["time"] = daily["slot"].apply(
    lambda value: (
        f"{int(value // 60):02d}:"
        f"{int(value % 60):02d}"
    )
)

st.subheader("Average traffic across 24 hours")
st.write(
    "Each point shows the average recorded vehicle count for that "
    "15-minute time slot across the available historical readings."
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
            scale=alt.Scale(domain=[0, 1440]),
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
            alt.Tooltip("reading_count:Q", title="Readings"),
            alt.Tooltip("distinct_days:Q", title="Days represented"),
        ],
    )
    .properties(height=350)
    .interactive()
)

st.altair_chart(chart, use_container_width=True)

with st.expander("How to read this graph"):
    st.write(
        "Lower points indicate fewer vehicles recorded on average "
        "during that time slot. Hover over a point to see its average, "
        "number of readings and number of dates represented."
    )
    st.write(
        "This is based on the available historical records, not live "
        "traffic or a direct measurement of journey delays."
    )


# --------------------------------------------------
# ROADWORKS DURATION AND BEST TIME
# --------------------------------------------------

st.divider()
st.subheader("When should you carry out roadworks?")
st.write(
    "Choose the required duration and permitted working hours. "
    "RoadworkWise will rank time windows by historical traffic levels."
)

with st.form("roadworks_schedule"):
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
            "Any day", "Monday", "Tuesday", "Wednesday",
            "Thursday", "Friday", "Saturday", "Sunday",
        ],
    )

    find_best = st.form_submit_button(
        "Find the best time",
        type="primary",
        use_container_width=True,
    )


def minute_of_day(value):
    return value.hour * 60 + value.minute


def format_time(value):
    return f"{int(value // 60):02d}:{int(value % 60):02d}"


if find_best:
    earliest_min = minute_of_day(earliest)
    finish_min = minute_of_day(latest_finish)
    duration_min = int(duration_hours * 60)

    if finish_min <= earliest_min:
        st.error(
            "The finish time must be later than the earliest start time."
        )
        st.stop()

    if earliest_min + duration_min > finish_min:
        st.error(
            "The roadworks duration does not fit in the permitted time range."
        )
        st.stop()

    data = camera_data.copy()

    if day_choice != "Any day":
        weekday_num = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ].index(day_choice)

        data = data[
            data["reading_time"].dt.dayofweek == weekday_num
        ].copy()

    if data.empty:
        st.warning(
            "There are no historical readings for that day of the week."
        )
        st.stop()

    data["date_only"] = data["reading_time"].dt.date
    data["minute"] = (
        data["reading_time"].dt.hour * 60
        + data["reading_time"].dt.minute
    )
    data["slot"] = (data["minute"] // 15) * 15

    # First average readings within each date and 15-minute slot.
    by_day = (
        data.groupby(["date_only", "slot"], as_index=False)
        .agg(vehicles=("vehicles", "mean"))
    )

    # Then average each slot across dates.
    profile = by_day.groupby("slot")["vehicles"].mean()

    candidates = []
    last_start = finish_min - duration_min

    for start in range(earliest_min, last_start + 1, 15):
        end = start + duration_min

        # Include all 15-minute slots touched by the work window.
        first_slot = (start // 15) * 15
        last_slot = ((end - 1) // 15) * 15
        slots = list(range(first_slot, last_slot + 1, 15))

        if not slots or not all(slot in profile.index for slot in slots):
            continue

        values = profile.loc[slots]

        candidates.append({
            "start": start,
            "end": end,
            "average_vehicles": float(values.mean()),
            "slots_covered": len(values),
        })

    if not candidates:
        st.warning(
            "There is not enough historical coverage for this duration "
            "and time range. Try a shorter duration or a wider time range."
        )
        st.stop()

    ranked = sorted(
        candidates,
        key=lambda item: item["average_vehicles"],
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

    st.subheader("Recommended roadworks times")
    st.success(
        "These options are ranked by the lowest average historical "
        "vehicle count across the selected time window."
    )

    for rank, item in enumerate(recommendations, start=1):
        with st.container(border=True):
            left, right = st.columns([3, 1])

            left.markdown(
                f"### {rank}. "
                f"{format_time(item['start'])}–"
                f"{format_time(item['end'])}"
            )
            left.caption(
                "Estimated historical traffic during the work window"
            )

            right.metric(
                "Average count per slot",
                f"{item['average_vehicles']:.1f}",
            )

    result_df = pd.DataFrame(recommendations)

    if not result_df.empty:
        result_df["start_time"] = result_df["start"].apply(format_time)
        result_df["finish_time"] = result_df["end"].apply(format_time)
        result_df["camera_id"] = selected_id
        result_df["camera_name"] = str(camera["sensor_name"])
        result_df["duration_hours"] = duration_hours
        result_df["day_choice"] = day_choice

        st.download_button(
            "Download recommendations",
            data=result_df.to_csv(index=False),
            file_name=f"roadworkwise_camera_{selected_id}.csv",
            mime="text/csv",
        )


# --------------------------------------------------
# DISCLAIMER
# --------------------------------------------------

st.caption(
    "Recommendations use historical vehicle counts from the most recent "
    "100,000 database readings loaded by the app. They do not measure live "
    "traffic, queue length or actual delays. Check road restrictions and "
    "current conditions before scheduling work."
)
