import os

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_PROVIDER_ORDER = "groq,openai"


PROVIDERS = {
    "groq": {
        "api_key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.1-8b-instant",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        "base_url": None,
    },
}


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _provider_order():
    raw_order = os.getenv("LLM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    order = [provider.strip().lower() for provider in raw_order.split(",")]
    order = [provider for provider in order if provider]

    unknown = [provider for provider in order if provider not in PROVIDERS]
    if unknown:
        valid = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"I do not know this LLM provider: {', '.join(unknown)}. "
            f"Use one of these: {valid}"
        )

    if not order:
        raise RuntimeError("LLM_PROVIDER_ORDER is empty")

    return order


def _client_for_provider(config):
    api_key = os.getenv(config["api_key_env"])
    if config["base_url"]:
        return OpenAI(api_key=api_key, base_url=config["base_url"])
    return OpenAI(api_key=api_key)


def complete_chat(messages):
    errors = []

    for provider_name in _provider_order():
        config = PROVIDERS[provider_name]
        if not os.getenv(config["api_key_env"]):
            errors.append(f"{provider_name}: missing {config['api_key_env']}")
            continue

        client = _client_for_provider(config)
        model = os.getenv(config["model_env"], config["default_model"])

        try:
            response = client.chat.completions.create(
                model=model,
                messages=list(messages),
            )
        except Exception as exc:
            errors.append(f"{provider_name}: {type(exc).__name__}: {exc}")
            continue

        content = response.choices[0].message.content
        return content or ""

    detail = "; ".join(errors) if errors else "no providers were tried"
    raise RuntimeError(f"I could not get a reply from any LLM provider ({detail})")
