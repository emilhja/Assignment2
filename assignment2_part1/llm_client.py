"""Groq-backed OpenAI-compatible chat completion client."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import load_dotenv
from openai import OpenAI


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"

# Read GROQ_API_KEY and optional model settings from this folder's .env file.
load_dotenv(Path(__file__).with_name(".env"))


def _groq_client() -> OpenAI:
    """Create an OpenAI SDK client that sends requests to Groq."""

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")

    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)


def complete_chat(messages: Sequence[Mapping[str, str]]) -> str:
    """Send messages to Groq and return only the assistant's text."""

    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    response = _groq_client().chat.completions.create(
        model=model,
        messages=list(messages),
    )
    return response.choices[0].message.content or ""
