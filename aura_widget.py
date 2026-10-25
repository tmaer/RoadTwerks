"""
AURA — RoadWork AI voice assistant, as a reusable module.

Use it in ANY Streamlit page as a floating chat bubble (bottom-right corner):

    from aura_widget import render_aura_widget
    render_aura_widget()          # call it near the top, BEFORE any st.stop()

Or as a full page:  see aura_app.py

voice --ElevenLabs STT--> text --Gemini (tools)--> Snowflake data
answer --ElevenLabs TTS--> voice (+ captions)
"""
import hashlib
import os
import re
from io import BytesIO

import streamlit as st
from dotenv import load_dotenv

from roadworkwise_core import evaluate_roadwork

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_v4")
STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
MAX_ROWS = 50

SYSTEM = """You are AURA, the accessible voice assistant of RoadWork AI.
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
For questions about WHEN to do roadworks, use best_roadwork_time.
If the user mentions a camera/sensor ID, filter by DETECTORS.ID."""


# ---------- connections (created once, shared) ----------
@st.cache_resource
def snowflake_conn():
    import snowflake.connector
    login = ({"password": os.environ["SNOWFLAKE_PASSWORD"]} if os.getenv("SNOWFLAKE_PASSWORD")
             else {"authenticator": "externalbrowser"})
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
    from elevenlabs.client import ElevenLabs
    return ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])


@st.cache_resource
def gemini():
    from google import genai
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
        st.session_state.aura_last_table = df
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
    st.session_state.aura_last_table = res
    best, worst = res.iloc[0], res.iloc[-1]
    return (f"Best start {best['start']}: +{best['extra_min_per_trip']} min per trip, "
            f"{best['total_hours_lost']} vehicle-hours lost. "
            f"Worst start {worst['start']}: +{worst['extra_min_per_trip']} min per trip, "
            f"{worst['total_hours_lost']} vehicle-hours lost.\n"
            f"All options (best first):\n{res.head(8).to_csv(index=False)}")


# ---------- voice + LLM ----------
def transcribe(audio_bytes: bytes) -> str:
    out = eleven().speech_to_text.convert(file=BytesIO(audio_bytes), model_id=STT_MODEL)
    return out.text.strip()


def speak(text: str) -> bytes:
    audio = eleven().text_to_speech.convert(text=text, voice_id=VOICE_ID, model_id=TTS_MODEL,
                                            output_format="mp3_44100_128")
    return b"".join(audio)


def _chat():
    if "aura_chat" not in st.session_state:
        from google.genai import types
        st.session_state.aura_chat = gemini().chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(system_instruction=SYSTEM,
                                               tools=[run_sql, best_roadwork_time]))
    return st.session_state.aura_chat


def llm_reply(question: str) -> str:
    return _chat().send_message(question).text or "Sorry, I couldn't find an answer."


def _init_state():
    st.session_state.setdefault("aura_messages", [])
    st.session_state.setdefault("aura_audio", None)
    st.session_state.setdefault("aura_audio_hash", None)
    st.session_state.setdefault("aura_last_table", None)


def ask(question: str, context: str = ""):
    """Send a question to AURA. `context` is extra info from the host page (e.g. selected camera)."""
    _init_state()
    prompt = f"[Context from the app: {context}]\n{question}" if context else question
    try:
        reply = llm_reply(prompt)
    except Exception as e:
        reply = f"Sorry, something went wrong: {e}"
    st.session_state.aura_messages += [("user", question), ("assistant", reply)]
    try:
        st.session_state.aura_audio = speak(reply)
    except Exception:
        st.session_state.aura_audio = None


def handle_inputs(voice, typed, context=""):
    if voice is not None:
        h = hashlib.md5(voice.getvalue()).hexdigest()
        if h != st.session_state.aura_audio_hash:   # only process new recordings
            st.session_state.aura_audio_hash = h
            with st.spinner("Listening…"):
                try:
                    question = transcribe(voice.getvalue())
                except Exception as e:
                    question = ""
                    st.error(f"Could not transcribe: {e}")
            if question:
                with st.spinner("AURA is checking the data…"):
                    ask(question, context)
    if typed:
        with st.spinner("AURA is checking the data…"):
            ask(typed, context)


# ---------- floating widget ----------
_CSS = """<style>
.st-key-aura_fab {
  position: fixed !important; right: 28px; bottom: 28px; z-index: 1000;
  width: auto !important;
}
.st-key-aura_fab button {
  width: 64px; height: 64px; border-radius: 50% !important; padding: 0 !important;
  background: linear-gradient(135deg, #2b6cff, #7b3dff) !important; color: #fff !important;
  border: none !important; font-size: 28px !important;
  box-shadow: 0 8px 24px rgba(43,108,255,.45);
  transition: transform .15s ease;
}
.st-key-aura_fab button:hover { transform: scale(1.07); }
.st-key-aura_fab button p { font-size: 30px !important; line-height: 1; margin: 0; }
.st-key-aura_fab button [data-testid="stIconMaterial"] { display: none; }   /* hide chevron */
[data-testid="stPopoverBody"]:has(.st-key-aura_panel) {
  width: min(400px, 92vw) !important; max-width: min(400px, 92vw) !important;
}
.st-key-aura_msgs .stChatMessage p { font-size: 1.05rem; line-height: 1.5; }
</style>"""


@st.fragment
def _panel(context_fn=None):
    """Chat panel. A fragment: chatting reruns only this panel, not the host page."""
    _init_state()
    with st.container(key="aura_panel"):
        st.markdown("**🎙️ AURA** · RoadWork AI assistant")
        st.caption("Speak or type. Every answer is spoken and captioned.")

        msgs = st.container(height=300, key="aura_msgs")
        voice = st.audio_input("🎤 Talk to AURA", key="aura_voice", label_visibility="collapsed")
        typed = st.chat_input("Ask about traffic or roadworks…", key="aura_text")

        context = context_fn() if context_fn else ""
        handle_inputs(voice, typed, context)

        with msgs:
            if not st.session_state.aura_messages:
                st.caption("Try: “What's the best time to close a lane on Castellana on Tuesday?”")
            for role, text in st.session_state.aura_messages:
                with st.chat_message(role, avatar="🧑" if role == "user" else "🎙️"):
                    st.write(text)

        if st.session_state.aura_audio:
            st.audio(st.session_state.aura_audio, format="audio/mp3", autoplay=True)
        if st.session_state.aura_last_table is not None:
            with st.expander("📊 Data behind the last answer"):
                st.dataframe(st.session_state.aura_last_table, use_container_width=True)


def render_aura_widget(context_fn=None):
    """Floating 🎙️ button in the bottom-right corner that opens the AURA chat.

    context_fn: optional function returning a short text about what the user is
                looking at (e.g. the selected camera), sent to AURA with each question.
    """
    st.markdown(_CSS, unsafe_allow_html=True)
    with st.container(key="aura_fab"):
        with st.popover("🎙️", help="Ask AURA"):
            _panel(context_fn)
