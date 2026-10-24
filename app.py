
import streamlit as st

# Page setup
st.set_page_config(
    page_title="RoadworkWise",
    page_icon="🚧",
    layout="wide"
)

# Main heading
st.title("🚧 RoadworkWise")
st.write(
    "Find the least disruptive time to schedule motorway roadworks "
    "using historical traffic data."
)

st.divider()

# User inputs
st.header("Plan your roadworks")

col1, col2 = st.columns(2)

with col1:
    motorway = st.selectbox(
        "Select a motorway",
        ["M1", "M4", "M25", "M40", "M5", "M6"]
    )

with col2:
    duration = st.slider(
        "Roadworks duration (hours)",
        min_value=1,
        max_value=12,
        value=4
    )

# Display current selections
st.subheader("Your selection")
st.write(f"**Motorway:** {motorway}")
st.write(f"**Roadworks duration:** {duration} hours")

# Placeholder for future recommendations
if st.button("Find the best time"):
    st.info(
        "Recommendations will appear here once historical "
        "traffic data is connected."
    )
