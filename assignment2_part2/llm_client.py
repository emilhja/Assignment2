"""Chat client that can try Groq first and OpenAI as a fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from openai import OpenAI
from dotenv import load_dotenv


# LLM_PROVIDER_ORDER can change which provider is tried first.
DEFAULT_PROVIDER_ORDER = "groq,openai"


@dataclass(frozen=True)
class ProviderConfig:
    """Settings needed to call one chat provider."""

    name: str
    api_key_env: str
    model_env: str
    default_model: str
    base_url: str | None = None


PROVIDERS: dict[str, ProviderConfig] = {
    "groq": ProviderConfig(
        name="groq",
        api_key_env="GROQ_API_KEY",
        model_env="GROQ_MODEL",
        default_model="llama-3.1-8b-instant",
        base_url="https://api.groq.com/openai/v1",
    ),
    "openai": ProviderConfig(
        name="openai",
        api_key_env="OPENAI_API_KEY",
        model_env="OPENAI_MODEL",
        default_model="gpt-4o-mini",
    ),
}


# Read API keys and optional model settings from this folder's .env file.
load_dotenv(Path(__file__).with_name(".env"))


def _provider_order() -> list[str]:
    """Return the provider names in the order they should be tried."""

    # Split names like "groq,openai" and ignore extra commas or spaces.
    raw_order = os.getenv("LLM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    order = [provider.strip().lower() for provider in raw_order.split(",")]
    order = [provider for provider in order if provider]

    # Stop early if the env var names a provider this file does not know.
    unknown = [provider for provider in order if provider not in PROVIDERS]
    if unknown:
        valid = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"Unknown provider(s) in LLM_PROVIDER_ORDER: {', '.join(unknown)}. "
            f"Valid providers: {valid}."
        )

    if not order:
        raise RuntimeError("LLM_PROVIDER_ORDER does not contain any providers.")

    return order


def _client_for_provider(config: ProviderConfig) -> OpenAI:
    """Create an OpenAI SDK client for the selected provider."""

    api_key = os.getenv(config.api_key_env)
    # Groq uses the OpenAI SDK, but sends requests to Groq's base URL.
    if config.base_url:
        return OpenAI(api_key=api_key, base_url=config.base_url)
    return OpenAI(api_key=api_key)


def complete_chat(messages: Sequence[Mapping[str, str]]) -> str:
    """Send messages and return only the assistant's text."""

    errors: list[str] = []

    # Try each provider in order, so a missing key or failed request can fall back.
    for provider_name in _provider_order():
        config = PROVIDERS[provider_name]
        if not os.getenv(config.api_key_env):
            errors.append(f"{config.name}: {config.api_key_env} is not set")
            continue

        client = _client_for_provider(config)
        model = os.getenv(config.model_env, config.default_model)

        try:
            # The parser needs the assistant text exactly as the model returned it.
            response = client.chat.completions.create(
                model=model,
                messages=list(messages),
            )
        except Exception as exc:
            errors.append(f"{config.name}: {type(exc).__name__}: {exc}")
            continue

        content = response.choices[0].message.content
        return content or ""

    # Show every provider error so setup problems are easier to diagnose.
    detail = "; ".join(errors) if errors else "no providers were attempted"
    raise RuntimeError(f"No configured LLM provider succeeded ({detail}).")
