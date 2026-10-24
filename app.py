
import os
import asyncio
from datetime import time

import asyncpg
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="RoadworkWise",
    page_icon="🚧",
    layout="wide",
)

st.title("🚧 RoadworkWise")
st.write(
    "Find quieter historical traffic windows for planned roadworks."
)


# --------------------------------------------------
# DATABASE CONNECTION
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
            timeout=15,
            command_timeout=45,
        ),
        timeout=20,
    )

    try:
        return await conn.fetch(sql, *args, timeout=45)
    finally:
        await conn.close()


# --------------------------------------------------
# LOAD TRAFFIC DATA
# --------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def load_traffic_data():
    sql = """
        SELECT
            t.date AS reading_time,
            t.id AS sensor_id,
            d.name AS sensor_name,
            d.district AS district,
            d.latitude AS latitude,
            d.longitude AS longitude,
            t.vehicles AS vehicles,
            t.intensity AS intensity,
            t.occupancy AS occupancy,
            t.load AS traffic_load
        FROM public.traffic AS t
        JOIN public.detectors AS d
            ON t.id = d.id
        WHERE t.date IS NOT NULL
          AND t.vehicles IS NOT NULL
        ORDER BY t.date DESC
        LIMIT 100000
    """

    rows = asyncio.run(query_database(sql))
    return pd.DataFrame([dict(row) for row in rows])


# --------------------------------------------------
# SIDEBAR SETTINGS
# --------------------------------------------------

with st.sidebar:
    st.header("Roadworks settings")

    if st.button("Refresh data from database"):
        st.cache_data.clear()
        st.rerun()

    duration = st.selectbox(
        "Roadworks duration",
        [30, 60, 90, 120, 180, 240, 360],
        index=1,
        format_func=lambda x: f"{x} minutes",
    )

    earliest_start = st.time_input(
        "Earliest start time",
        value=time(6, 0),
    )

    latest_finish = st.time_input(
        "Latest finish time",
        value=time(22, 0),
    )

    number_of_recommendations = st.slider(
        "Number of recommendations",
        min_value=1,
        max_value=5,
        value=3,
    )


# --------------------------------------------------
# FETCH DATA
# --------------------------------------------------

st.subheader("Traffic database")

try:
    with st.spinner("Connecting to TigerData and loading readings..."):
        df = load_traffic_data()

except Exception as e:
    st.error("Could not load traffic data.")
    st.write(
        "Check your database credentials in the .env file, "
        "your internet connection, and the database query."
    )
    st.exception(e)
    st.stop()


if df.empty:
    st.warning(
        "The database query returned no readings. "
        "Check that public.traffic contains data and that "
        "traffic sensor IDs match public.detectors IDs."
    )
    st.stop()


df["reading_time"] = pd.to_datetime(
    df["reading_time"], errors="coerce"
)

df["vehicles"] = pd.to_numeric(
    df["vehicles"], errors="coerce"
)

df = df.dropna(
    subset=["reading_time", "vehicles", "sensor_id"]
)

if df.empty:
    st.warning("No valid traffic readings were returned.")
    st.stop()


# --------------------------------------------------
# OVERVIEW
# --------------------------------------------------

first_reading = df["reading_time"].min()
latest_reading = df["reading_time"].max()

col1, col2, col3 = st.columns(3)

col1.metric(
    "Sensors",
    df["sensor_id"].nunique(),
)

col2.metric(
    "Traffic readings loaded",
    f"{len(df):,}",
)

col3.metric(
    "Average vehicles per reading",
    f"{df['vehicles'].mean():.1f}",
)

st.caption(
    f"Loaded data range: {first_reading:%d %b %Y %H:%M} "
    f"to {latest_reading:%d %b %Y %H:%M}. "
    "The database may contain older readings beyond this sample."
)


# --------------------------------------------------
# SELECT SENSOR
# --------------------------------------------------

st.header("1. Select a road location")

sensors = (
    df[
        [
            "sensor_id",
            "sensor_name",
            "district",
            "latitude",
            "longitude",
        ]
    ]
    .drop_duplicates(subset=["sensor_id"])
    .sort_values("sensor_id")
    .copy()
)

sensors["label"] = sensors.apply(
    lambda row: (
        f"{row['sensor_name']} "
        f"(Sensor {row['sensor_id']}, District {row['district']})"
    ),
    axis=1,
)

selected_label = st.selectbox(
    "Choose a traffic sensor",
    sensors["label"].tolist(),
)

selected_sensor = sensors[
    sensors["label"] == selected_label
].iloc[0]

sensor_id = selected_sensor["sensor_id"]

sensor_df = df[
    df["sensor_id"] == sensor_id
].copy()

col1, col2, col3 = st.columns(3)

col1.write(f"**Sensor:** {selected_sensor['sensor_name']}")
col2.write(f"**District:** {selected_sensor['district']}")
col3.write(
    f"**Coordinates:** "
    f"{selected_sensor['latitude']}, "
    f"{selected_sensor['longitude']}"
)


# --------------------------------------------------
# HISTORICAL TRAFFIC CHART
# --------------------------------------------------

st.header("2. Historical traffic patterns")

sensor_df["time_slot"] = (
    sensor_df["reading_time"].dt.hour * 60
    + sensor_df["reading_time"].dt.minute
) // 15

average_traffic = (
    sensor_df.groupby("time_slot")["vehicles"]
    .mean()
    .reset_index()
)

average_traffic["Time"] = average_traffic["time_slot"].apply(
    lambda slot: (
        f"{int(slot * 15 // 60):02d}:"
        f"{int(slot * 15 % 60):02d}"
    )
)

if not average_traffic.empty:
    st.line_chart(
        average_traffic.set_index("Time")[["vehicles"]]
    )

st.caption(
    "Average recorded vehicles in each 15-minute time slot, "
    "using the readings currently loaded."
)


# --------------------------------------------------
# FIND QUIETER WINDOWS
# --------------------------------------------------

st.header("3. Recommended roadworks windows")

if earliest_start >= latest_finish:
    st.error(
        "The earliest start time must be before the latest finish time."
    )
    st.stop()


if st.button("Find quieter time windows", type="primary"):

    slot_minutes = 15

    # Round duration up to a whole number of 15-minute slots.
    slots_needed = max(
        1,
        (duration + slot_minutes - 1) // slot_minutes,
    )

    start_minute = (
        earliest_start.hour * 60 + earliest_start.minute
    )

    finish_minute = (
        latest_finish.hour * 60 + latest_finish.minute
    )

    # Historical mean vehicle count for each daily time slot.
    slot_traffic = (
        sensor_df.groupby("time_slot")["vehicles"]
        .mean()
        .to_dict()
    )

    candidates = []

    first_slot = (start_minute + 14) // 15
    last_slot = finish_minute // 15

    for start_slot in range(first_slot, last_slot):
        end_slot = start_slot + slots_needed

        if end_slot * 15 > finish_minute:
            continue

        window_slots = list(
            range(start_slot, end_slot)
        )

        # Only rank windows with data for every slot.
        if not all(
            slot in slot_traffic
            for slot in window_slots
        ):
            continue

        traffic_score = sum(
            slot_traffic[slot]
            for slot in window_slots
        )

        candidates.append({
            "start_slot": start_slot,
            "end_slot": end_slot,
            "traffic_score": traffic_score,
            "average_vehicles": (
                traffic_score / slots_needed
            ),
        })

    candidates.sort(
        key=lambda item: item["traffic_score"]
    )

    recommendations = []

    for candidate in candidates:
        overlaps = any(
            candidate["start_slot"] < existing["end_slot"]
            and candidate["end_slot"] > existing["start_slot"]
            for existing in recommendations
        )

        if not overlaps:
            recommendations.append(candidate)

        if len(recommendations) >= number_of_recommendations:
            break

    if not recommendations:
        st.warning(
            "No suitable windows found. Try a shorter duration "
            "or a wider time range. This sensor needs historical "
            "readings throughout the proposed windows."
        )

    else:
        for rank, recommendation in enumerate(
            recommendations, start=1
        ):
            start = recommendation["start_slot"] * 15
            end = recommendation["end_slot"] * 15

            start_text = (
                f"{start // 60:02d}:{start % 60:02d}"
            )
            end_text = (
                f"{end // 60:02d}:{end % 60:02d}"
            )

            st.subheader(
                f"Option {rank}: {start_text} – {end_text}"
            )

            st.write(
                f"**Average vehicles per 15-minute slot:** "
                f"{recommendation['average_vehicles']:.1f}"
            )

            st.write(
                f"**Estimated duration:** "
                f"{slots_needed * 15} minutes"
            )

        st.info(
            "These options are ranked using historical vehicle counts. "
            "They estimate traffic volume, not actual delays or queues, "
            "and historical patterns cannot guarantee future conditions."
        )


# --------------------------------------------------
# VIEW READINGS
# --------------------------------------------------

with st.expander("View recent traffic readings"):
    st.dataframe(
        sensor_df.sort_values(
            "reading_time", ascending=False
        ).head(100),
        use_container_width=True,
    )
