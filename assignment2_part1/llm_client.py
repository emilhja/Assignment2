import os

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"

# If no models is chosen in .env then above model is used, whic had been tested.
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)


def _groq_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY in .env before running the agent")

    return OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")


def complete_chat(messages):
    client = _groq_client()
    model_name = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

    completion = client.chat.completions.create(
        model=model_name,
        messages=messages,
    )

    message = completion.choices[0].message
    return message.content or ""
