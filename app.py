
import asyncio
import os
from datetime import date

import asyncpg
import folium
import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium

load_dotenv()

st.set_page_config(
    page_title="RoadworkWise",
    page_icon="🚧",
    layout="wide",
)

st.title("🚧 RoadworkWise")
st.caption(
    "Find quieter times to schedule roadworks using historical traffic data."
)

# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def get_db_settings():
    required = [
        "DB_USER",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
    ]

    missing = [key for key in required if not os.getenv(key)]

    if missing:
        raise ValueError(
            "Missing database settings in .env: "
            + ", ".join(missing)
        )

    return {
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "host": os.getenv("DB_HOST"),
        "port": int(os.getenv("DB_PORT")),
        "database": os.getenv("DB_NAME"),
        "ssl": os.getenv("DB_SSL", "require"),
        "timeout": 15,
    }


async def fetch_tables():
    conn = await asyncpg.connect(**get_db_settings())

    try:
        rows = await conn.fetch("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)

        return [row["table_name"] for row in rows]

    finally:
        await conn.close()


async def fetch_columns(table_name):
    conn = await asyncpg.connect(**get_db_settings())

    try:
        rows = await conn.fetch("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1
            ORDER BY ordinal_position
        """, table_name)

        return [
            {"name": row["column_name"], "type": row["data_type"]}
            for row in rows
        ]

    finally:
        await conn.close()


async def fetch_table_data(table_name, columns):
    conn = await asyncpg.connect(**get_db_settings())

    try:
        # Column and table names come from database metadata.
        # Double quotes safely handle identifiers.
        quoted_table = '"' + table_name.replace('"', '""') + '"'
        quoted_columns = ", ".join(
            '"' + col.replace('"', '""') + '"'
            for col in columns
        )

        query = (
            f"SELECT {quoted_columns} "
            f"FROM {quoted_table} LIMIT 100000"
        )

        rows = await conn.fetch(query)

        return [dict(row) for row in rows]

    finally:
        await conn.close()


@st.cache_data(ttl=300)
def load_tables():
    return asyncio.run(fetch_tables())


@st.cache_data(ttl=300)
def load_columns(table_name):
    return asyncio.run(fetch_columns(table_name))


@st.cache_data(ttl=300)
def load_data(table_name, columns):
    rows = asyncio.run(fetch_table_data(table_name, columns))
    return pd.DataFrame(rows)


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def find_column(columns, candidates):
    """Find a column without depending on uppercase/lowercase."""
    lookup = {col.lower(): col for col in columns}

    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]

    return None


def make_sensor_label(row):
    return (
        f"{row['latitude']:.5f}, {row['longitude']:.5f}"
    )


def calculate_forecast(data, timestamp_col, roadworks_date):
    """Calculate historical mean traffic by hour and weekday."""

    df = data.copy()

    df[timestamp_col] = pd.to_datetime(
        df[timestamp_col],
        errors="coerce",
        utc=True,
    )

    df["vehicle_count"] = pd.to_numeric(
        df["vehicle_count"],
        errors="coerce",
    )

    df = df.dropna(
        subset=[timestamp_col, "vehicle_count"]
    )

    if df.empty:
        return None

    # Use UTC consistently so mixed timestamp time zones
    # do not cause inconsistent comparisons.
    df["hour"] = df[timestamp_col].dt.hour
    df["weekday"] = df[timestamp_col].dt.dayofweek

    # Prefer the selected weekday where enough observations exist.
    weekday_data = df[
        df["weekday"] == roadworks_date.weekday()
    ]

    if weekday_data["hour"].nunique() >= 3:
        source = weekday_data
        method = "Historical observations from the same weekday"
    else:
        source = df
        method = "All available historical observations"

    hourly = (
        source.groupby("hour")["vehicle_count"]
        .mean()
        .reindex(range(24))
    )

    # Fill missing hours using the overall historical hourly mean.
    overall = (
        df.groupby("hour")["vehicle_count"]
        .mean()
        .reindex(range(24))
    )

    hourly = hourly.fillna(overall)

    return hourly, method, len(df)


def find_quiet_windows(hourly, duration, earliest, latest):
    """Rank start times by total expected vehicles during roadworks."""

    results = []

    # A window of duration hours must finish by the latest
    # allowed finish hour. Windows do not cross midnight.
    for start in range(earliest, latest - duration + 1):
        end = start + duration

        values = hourly.iloc[start:end]

        if values.isna().any():
            continue

        results.append({
            "Start": start,
            "End": end,
            "Estimated vehicles": float(values.sum()),
            "Average vehicles/hour": float(values.mean()),
        })

    results.sort(key=lambda item: item["Estimated vehicles"])

    # Avoid recommending three almost-identical overlapping windows.
    selected = []

    for result in results:
        overlaps = any(
            result["Start"] < existing["End"]
            and existing["Start"] < result["End"]
            for existing in selected
        )

        if not overlaps:
            selected.append(result)

        if len(selected) == 3:
            break

    return selected


def format_hour(hour):
    return f"{hour:02d}:00"


# --------------------------------------------------
# SIDEBAR: CONNECTION AND ROADWORK SETTINGS
# --------------------------------------------------

with st.sidebar:
    st.header("Roadworks settings")

    roadworks_date = st.date_input(
        "Planned date",
        value=date.today(),
    )

    duration = st.slider(
        "Roadworks duration (hours)",
        min_value=1,
        max_value=12,
        value=2,
    )

    earliest = st.selectbox(
        "Earliest permitted start",
        options=list(range(24)),
        index=6,
        format_func=format_hour,
    )

    latest = st.selectbox(
        "Latest permitted finish",
        options=list(range(1, 25)),
        index=22,
        format_func=format_hour,
    )

    st.caption(
        "Recommendations use historical vehicle counts, "
        "not live traffic conditions."
    )

# --------------------------------------------------
# LOAD DATABASE TABLES
# --------------------------------------------------

try:
    tables = load_tables()

except Exception as exc:
    st.error("Could not connect to TigerData.")
    st.write(
        "Check the credentials in your .env file, "
        "your network connection and the database settings."
    )
    st.code(str(exc))
    st.stop()

if not tables:
    st.warning(
        "The database connection worked, but no tables were "
        "found in the public schema."
    )
    st.stop()

st.sidebar.header("Traffic data")

table_name = st.sidebar.selectbox(
    "Choose database table",
    options=tables,
)

try:
    metadata = load_columns(table_name)
except Exception as exc:
    st.error(f"Could not inspect table: {exc}")
    st.stop()

column_names = [col["name"] for col in metadata]

latitude_col = find_column(
    column_names,
    ["latitude", "lat"],
)

longitude_col = find_column(
    column_names,
    ["longitude", "lon", "lng"],
)

count_col = find_column(
    column_names,
    ["vehicle_count", "vehiclecount", "count", "traffic_count"],
)

timestamp_col = find_column(
    column_names,
    [
        "timestamp",
        "recorded_at",
        "datetime",
        "date_time",
        "measurement_time",
        "event_time",
        "time",
        "date",
        "ts",
    ],
)

if not latitude_col or not longitude_col or not count_col:
    st.error(
        "This table does not contain all the required columns."
    )
    st.write("Columns found:")
    st.code(", ".join(column_names))
    st.info(
        "Select a table containing latitude, longitude and "
        "vehicle_count, or update the column matching lists "
        "in app.py."
    )
    st.stop()

if not timestamp_col:
    st.error(
        "I could not identify a timestamp column. "
        "A timestamp is needed to calculate hourly forecasts."
    )
    st.write("Columns found:")
    st.code(", ".join(column_names))
    st.info(
        "Find the column containing the date and time of each "
        "traffic reading, then add its name to timestamp_col's "
        "candidate list in app.py."
    )
    st.stop()

with st.spinner("Loading traffic records..."):
    try:
        raw = load_data(table_name, column_names)
    except Exception as exc:
        st.error(f"Could not load the selected table: {exc}")
        st.stop()

if raw.empty:
    st.warning("The selected table contains no records.")
    st.stop()

# --------------------------------------------------
# STANDARDISE THE DATA
# --------------------------------------------------

data = raw.rename(columns={
    latitude_col: "latitude",
    longitude_col: "longitude",
    count_col: "vehicle_count",
    timestamp_col: "timestamp",
}).copy()

for col in ["latitude", "longitude", "vehicle_count"]:
    data[col] = pd.to_numeric(data[col], errors="coerce")

data["timestamp"] = pd.to_datetime(
    data["timestamp"],
    errors="coerce",
    utc=True,
)

data = data.dropna(
    subset=["latitude", "longitude", "vehicle_count", "timestamp"]
)

# Remove invalid coordinates and negative counts.
data = data[
    data["latitude"].between(-90, 90)
    & data["longitude"].between(-180, 180)
    & (data["vehicle_count"] >= 0)
].copy()

if data.empty:
    st.error(
        "No valid traffic records were found after cleaning the data."
    )
    st.stop()

# Use rounded coordinates to identify locations when there is no
# separate sensor ID column.
data["sensor_id"] = (
    data["latitude"].round(5).astype(str)
    + ", "
    + data["longitude"].round(5).astype(str)
)

sensors = (
    data.groupby("sensor_id")
    .agg(
        latitude=("latitude", "median"),
        longitude=("longitude", "median"),
        readings=("vehicle_count", "size"),
    )
    .reset_index()
    .sort_values("sensor_id")
)

st.sidebar.success(
    f"Loaded {len(data):,} records from {len(sensors)} locations."
)

# --------------------------------------------------
# SENSOR SELECTION
# --------------------------------------------------

st.subheader("1. Select a traffic sensor")

sensor_id = st.selectbox(
    "Sensor location",
    options=sensors["sensor_id"].tolist(),
)

sensor = sensors[sensors["sensor_id"] == sensor_id].iloc[0]

sensor_data = data[data["sensor_id"] == sensor_id].copy()

left, right = st.columns([1, 1])

with left:
    st.metric("Traffic readings", f"{len(sensor_data):,}")
    st.metric(
        "Sensor latitude",
        f"{sensor['latitude']:.5f}",
    )

with right:
    st.metric("Available sensors", f"{len(sensors)}")
    st.metric(
        "Sensor longitude",
        f"{sensor['longitude']:.5f}",
    )

# --------------------------------------------------
# MAP
# --------------------------------------------------

st.subheader("2. Traffic sensor map")

traffic_map = folium.Map(
    location=[
        float(sensor["latitude"]),
        float(sensor["longitude"]),
    ],
    zoom_start=12,
)

for _, point in sensors.iterrows():
    is_selected = point["sensor_id"] == sensor_id

    folium.CircleMarker(
        location=[
            float(point["latitude"]),
            float(point["longitude"]),
        ],
        radius=9 if is_selected else 5,
        tooltip=point["sensor_id"],
        popup=(
            f"Location: {point['sensor_id']}<br>"
            f"Readings: {point['readings']}"
        ),
        color="#e4572e" if is_selected else "#247ba0",
        fill=True,
        fill_opacity=0.8,
    ).add_to(traffic_map)

st_folium(
    traffic_map,
    height=420,
    width=None,
    key="roadworkwise_map",
)

# --------------------------------------------------
# FORECAST AND RECOMMENDATIONS
# --------------------------------------------------

st.subheader("3. Find the quietest roadworks times")

if earliest >= latest:
    st.error(
        "The earliest start must be before the latest finish."
    )
    st.stop()

if duration > latest - earliest:
    st.error(
        "The duration does not fit within the permitted time range. "
        "Increase the allowed window or shorten the duration."
    )
    st.stop()

forecast_result = calculate_forecast(
    sensor_data,
    "timestamp",
    roadworks_date,
)

if forecast_result is None:
    st.error("There is not enough valid traffic data to forecast.")
    st.stop()

hourly, method, valid_records = forecast_result

if hourly.isna().all():
    st.error("No usable hourly traffic values could be calculated.")
    st.stop()

recommendations = find_quiet_windows(
    hourly,
    duration,
    earliest,
    latest,
)

if not recommendations:
    st.warning(
        "No valid time windows were found. Check the duration, "
        "allowed hours and available historical readings."
    )
    st.stop()

st.caption(
    f"Forecast method: {method}. "
    f"Based on {valid_records:,} valid readings at this sensor."
)

metric1, metric2, metric3 = st.columns(3)

metric1.metric(
    "Quietest hour",
    format_hour(int(hourly.idxmin())),
)

metric2.metric(
    "Busiest hour",
    format_hour(int(hourly.idxmax())),
)

metric3.metric(
    "Mean vehicles/hour",
    f"{hourly.mean():,.1f}",
)

st.markdown("#### Historical hourly traffic pattern")

chart_data = pd.DataFrame({
    "Hour": [format_hour(h) for h in range(24)],
    "Estimated vehicles": hourly.values,
}).set_index("Hour")

st.line_chart(chart_data)

st.markdown("#### Recommended time windows")

for rank, item in enumerate(recommendations, start=1):
    start_text = format_hour(item["Start"])
    end_text = format_hour(item["End"])

    with st.container(border=True):
        st.markdown(
            f"**{rank}. {start_text}–{end_text}**"
        )

        col1, col2 = st.columns(2)

        col1.metric(
            "Estimated vehicles during works",
            f"{item['Estimated vehicles']:,.1f}",
        )

        col2.metric(
            "Average vehicles/hour",
            f"{item['Average vehicles/hour']:,.1f}",
        )

# --------------------------------------------------
# DATA PREVIEW AND LIMITATIONS
# --------------------------------------------------

with st.expander("View raw traffic records"):
    st.dataframe(
        sensor_data[
            [
                "timestamp",
                "latitude",
                "longitude",
                "vehicle_count",
            ]
        ].sort_values("timestamp", ascending=False),
        use_container_width=True,
    )

st.divider()

st.caption(
    "Important: these are historical traffic-count estimates, "
    "not guaranteed predictions of future traffic or roadworks delays. "
    "Results depend on the quality, coverage and time zone of the source data. "
    "The app currently ranks time windows by estimated total vehicle counts."
)
