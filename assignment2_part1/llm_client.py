import os

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_PROVIDER_ORDER = "groq"
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
LOCAL_PROVIDER_DUMMY_API_KEY = "local-llm"

# If no models is chosen in .env then above model is used, whic had been tested.
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)


def _groq_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY in .env before running the agent")

    return OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")


def _local_base_url():
    base_url = os.getenv("LOCAL_LLM_BASE_URL", DEFAULT_LOCAL_BASE_URL).strip().rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return base_url


def _local_client():
    api_key = os.getenv("LOCAL_LLM_API_KEY") or LOCAL_PROVIDER_DUMMY_API_KEY
    return OpenAI(api_key=api_key, base_url=_local_base_url())


def _provider_order():
    raw_order = os.getenv("LLM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    order = [provider.strip().lower() for provider in raw_order.split(",")]
    order = [provider for provider in order if provider]
    unknown = [provider for provider in order if provider not in {"groq", "local"}]
    if unknown:
        raise RuntimeError(
            f"I do not know this LLM provider: {', '.join(unknown)}. "
            "Use one of these: groq, local"
        )
    if not order:
        raise RuntimeError("LLM_PROVIDER_ORDER is empty")
    return order


def complete_chat(messages):
    errors = []

    for provider in _provider_order():
        if provider == "local":
            client = _local_client()
            model_name = os.getenv("LOCAL_LLM_MODEL", DEFAULT_LOCAL_MODEL)
        else:
            if not os.getenv("GROQ_API_KEY"):
                errors.append("groq: missing GROQ_API_KEY")
                continue
            client = _groq_client()
            model_name = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=messages,
            )
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {exc}")
            continue

        message = completion.choices[0].message
        return message.content or ""

    detail = "; ".join(errors) if errors else "no providers were tried"
    raise RuntimeError(f"I could not get a reply from any LLM provider ({detail})")
