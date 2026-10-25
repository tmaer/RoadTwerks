"""
AURA — the accessible voice assistant of RoadworkWise.

Speak (or type) a question about Madrid traffic or roadworks:
  voice --ElevenLabs speech-to-text--> text --Gemini--> tools --> Snowflake data
  answer --ElevenLabs text-to-speech--> voice  (+ big captions on screen)

Gemini has two tools:
  run_sql()            -> read-only SQL on the Snowflake traffic data
  best_roadwork_time() -> the RoadworkWise engine (best hour to close lanes)

Before:
  py -V:3.12 -m pip install streamlit google-genai elevenlabs "snowflake-connector-python[pandas]" python-dotenv
  Fill in the .env file (see .env.example) and run 8_upload_snowflake.py once.
Run:
  py -V:3.12 -m streamlit run aura_app.py
"""
import hashlib
import os
import re
from io import BytesIO

import pandas as pd
import snowflake.connector
import streamlit as st
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from google import genai
from google.genai import types

from roadworkwise_core import evaluate_roadwork

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")   # CHANGE to a newer model if you want
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # any voice from elevenlabs.io/app/voice-library
TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_v4")
STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
MAX_ROWS = 50

SYSTEM = """You are AURA, the accessible voice assistant of RoadworkWise.
You help city officials and citizens understand Madrid road traffic and plan roadworks
so they cause the least disruption. Your answers are READ ALOUD, so:
- reply in the same language the user speaks,
- keep it short (2-4 sentences), natural, no markdown, no tables, no bullet points,
- round numbers and say times like "nine in the morning",
- never invent data: always use your tools, and say so if the data doesn't cover the question.

Data (Snowflake, Madrid city traffic sensors, December 2025, one row per sensor per hour):
  DETECTORS(ID, NAME, DISTRICT, LATITUDE, LONGITUDE)  -- NAME is a street/sensor name, e.g. 'Pº Castellana ...'
  TRAFFIC_HOURLY(HOUR TIMESTAMP, ID, VEHICLES, INTENSITY, LOAD)
    VEHICLES = vehicles counted in that hour, INTENSITY = flow in vehicles/hour, LOAD = % of capacity.
Snowflake SQL tips: join on ID; search streets with NAME ILIKE '%castellana%';
HOUR(HOUR) gives the hour of day; DAYOFWEEKISO(HOUR) gives 1=Monday..7=Sunday.
For questions about WHEN to do roadworks, use best_roadwork_time."""


# ---------- connections ----------
@st.cache_resource
def snowflake_conn():
    login = ({"password": os.environ["SNOWFLAKE_PASSWORD"]} if os.getenv("SNOWFLAKE_PASSWORD")
             else {"authenticator": "externalbrowser"})   # no password -> browser login
    return snowflake.connector.connect(
        **login,
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "ROADWORKWISE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
    )


@st.cache_resource
def eleven():
    return ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])


@st.cache_resource
def gemini():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def query_df(sql, params=None):
    cur = snowflake_conn().cursor()
    cur.execute(sql, params)
    return cur.fetch_pandas_all()


# ---------- tools Gemini can call ----------
def run_sql(query: str) -> str:
    """Runs a read-only Snowflake SQL query on the Madrid traffic data and returns the rows as CSV.

    Args:
        query: A single SELECT (or WITH ... SELECT) statement using the DETECTORS and TRAFFIC_HOURLY tables.
    """
    q = query.strip().rstrip(";")
    if ";" in q or not re.match(r"(?is)^\s*(select|with)\b", q):
        return "Error: only a single SELECT query is allowed."
    try:
        df = query_df(f"SELECT * FROM ({q}) LIMIT {MAX_ROWS}")
        st.session_state.last_table = df
        return df.to_csv(index=False) if not df.empty else "No rows."
    except Exception as e:
        return f"SQL error: {e}"


def best_roadwork_time(street: str, weekday: int, total_lanes: int = 3,
                       closed_lanes: int = 1, duration_hours: int = 3,
                       length_km: float = 1.5) -> str:
    """Finds the least disruptive start time for roadworks on a Madrid street, using real sensor data.

    Args:
        street: Part of the street name, e.g. 'castellana'.
        weekday: Day of the week, 1 = Monday ... 7 = Sunday.
        total_lanes: Lanes in that direction (default 3).
        closed_lanes: Lanes closed by the works (default 1).
        duration_hours: How long the works last, in hours (default 3).
        length_km: Length of the affected section in km (default 1.5).
    """
    try:
        df = query_df("""
            SELECT HOUR(t.HOUR) AS H, AVG(t.INTENSITY) AS V
            FROM TRAFFIC_HOURLY t JOIN DETECTORS d ON t.ID = d.ID
            WHERE d.NAME ILIKE %s AND DAYOFWEEKISO(t.HOUR) = %s
            GROUP BY H""", (f"%{street}%", int(weekday)))
    except Exception as e:
        return f"Data error: {e}"
    if df.empty:
        return f"No sensors found on a street matching '{street}'."
    vol = df.set_index("H")["V"].astype(float).reindex(range(24), fill_value=0)
    res = evaluate_roadwork(vol, total_lanes, closed_lanes, int(duration_hours), length_km)
    st.session_state.last_table = res
    best, worst = res.iloc[0], res.iloc[-1]
    return (f"Best start {best['start']}: +{best['extra_min_per_trip']} min per trip, "
            f"{best['total_hours_lost']} vehicle-hours lost. "
            f"Worst start {worst['start']}: +{worst['extra_min_per_trip']} min per trip, "
            f"{worst['total_hours_lost']} vehicle-hours lost.\n"
            f"All options (best first):\n{res.head(8).to_csv(index=False)}")


# ---------- voice ----------
def transcribe(audio_bytes: bytes) -> str:
    out = eleven().speech_to_text.convert(file=BytesIO(audio_bytes), model_id=STT_MODEL)
    return out.text.strip()


def speak(text: str) -> bytes:
    audio = eleven().text_to_speech.convert(text=text, voice_id=VOICE_ID, model_id=TTS_MODEL,
                                            output_format="mp3_44100_128")
    return b"".join(audio)


def ask(question: str):
    chat = st.session_state.chat
    reply = chat.send_message(question).text or "Sorry, I couldn't find an answer."
    st.session_state.messages += [("user", question), ("assistant", reply)]
    try:
        st.session_state.audio = speak(reply)
    except Exception as e:
        st.session_state.audio = None
        st.warning(f"Voice unavailable: {e}")


# ---------- page ----------
st.set_page_config(page_title="AURA · RoadworkWise", page_icon="🎙️", layout="centered")
st.markdown("""<style>
  .stChatMessage p {font-size: 1.35rem; line-height: 1.6;}
  h1 {letter-spacing: .04em;}
</style>""", unsafe_allow_html=True)

st.title("🎙️ AURA")
st.caption("RoadworkWise voice assistant · Madrid traffic data on Snowflake · "
           "Gemini · ElevenLabs. Speak or type — every answer is spoken and captioned.")

if "chat" not in st.session_state:
    st.session_state.chat = gemini().chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(system_instruction=SYSTEM,
                                           tools=[run_sql, best_roadwork_time]))
    st.session_state.messages, st.session_state.audio = [], None
    st.session_state.last_audio_hash, st.session_state.last_table = None, None

voice = st.audio_input("🎤 Press to talk to AURA")
typed = st.chat_input("…or type your question")

if voice is not None:
    h = hashlib.md5(voice.getvalue()).hexdigest()
    if h != st.session_state.last_audio_hash:          # only process new recordings
        st.session_state.last_audio_hash = h
        with st.spinner("Listening…"):
            question = transcribe(voice.getvalue())
        if question:
            with st.spinner("AURA is checking the data…"):
                ask(question)
if typed:
    with st.spinner("AURA is checking the data…"):
        ask(typed)

for role, text in st.session_state.messages:
    with st.chat_message(role, avatar="🧑" if role == "user" else "🎙️"):
        st.write(text)

if st.session_state.audio:
    st.audio(st.session_state.audio, format="audio/mp3", autoplay=True)

if st.session_state.last_table is not None:
    with st.expander("📊 Data behind the last answer"):
        st.dataframe(st.session_state.last_table, use_container_width=True)
