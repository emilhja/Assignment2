import os

from dotenv import load_dotenv
from openai import OpenAI


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _groq_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")

    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)


def complete_chat(messages):
    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    response = _groq_client().chat.completions.create(
        model=model,
        messages=messages,
    )
    return response.choices[0].message.content or ""
