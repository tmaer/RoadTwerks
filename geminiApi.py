
import os 
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key = os.environ["SNOWFLAKE_PAT"],
                base_url = f"https://{os.environ['SNOWFLAKE_ACCOUNT']}.snowflakecomputing.com/api/v2/cortex/v1",)

MODEL = "gemini-3-pro"
history = [{"role": "system","content" : "You are a helpful assistant."}]


def chat(user_mg:str) -> str:
    history.append({"role" : "user","content" : user_mg})
    stream = client.chat.completions.create(model = MODEL,messages = history,stream=True)
    reply = ""
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            piece = chunk.choices[0].delta.content
            print(piece, end = "", flush = True)
            reply += piece

    print()
    history.append({"role" : "assistant","content" : reply})
    return reply


while (msg := input("You: ")) not in ("quit","exit"):
    chat(msg)
