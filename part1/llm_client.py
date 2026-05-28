import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_PROVIDER_ORDER = "groq"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
LOCAL_DUMMY_KEY = "local-llm"

load_dotenv(Path(__file__).with_name(".env"))


def _provider_order() -> list[str]:
    raw = os.getenv("LLM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    providers = [item.strip().lower() for item in raw.split(",") if item.strip()]
    unknown = [item for item in providers if item not in {"groq", "local"}]
    if unknown:
        raise RuntimeError(f"Unknown LLM provider: {', '.join(unknown)}")
    if not providers:
        raise RuntimeError("LLM_PROVIDER_ORDER is empty")
    return providers


def _local_base_url() -> str:
    base_url = os.getenv("LOCAL_LLM_BASE_URL", DEFAULT_LOCAL_BASE_URL).strip().rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return base_url


def _llm_timeout_seconds() -> float:
    raw = os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise RuntimeError("LLM_TIMEOUT_SECONDS must be a number") from exc
    if timeout <= 0:
        raise RuntimeError("LLM_TIMEOUT_SECONDS must be greater than zero")
    return timeout


def _client_for(provider: str) -> tuple[OpenAI, str]:
    if provider == "local":
        client = OpenAI(
            api_key=os.getenv("LOCAL_LLM_API_KEY") or LOCAL_DUMMY_KEY,
            base_url=_local_base_url(),
            timeout=_llm_timeout_seconds(),
        )
        return client, os.getenv("LOCAL_LLM_MODEL", DEFAULT_LOCAL_MODEL)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("missing GROQ_API_KEY")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
        timeout=_llm_timeout_seconds(),
    )
    return client, os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)


def complete_chat(messages: list[dict[str, str]]) -> str:
    errors: list[str] = []
    for provider in _provider_order():
        try:
            client, model = _client_for(provider)
            completion = client.chat.completions.create(model=model, messages=messages)
            return completion.choices[0].message.content or ""
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"No LLM provider succeeded ({'; '.join(errors)})")
