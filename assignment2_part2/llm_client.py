import ast
import json
import os
import random
import re
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

import colors


DEFAULT_PROVIDER_ORDER = "groq,openai"
RATE_LIMIT_RETRY_WAIT = 10  # fallback when the provider gives no hint
RATE_LIMIT_MAX_WAIT = 60    # cap any single sleep
RATE_LIMIT_JITTER_MAX = 5   # spread agents across the reset window
DEFAULT_RATE_LIMIT_TOTAL_WAIT = 0  # 0 means wait indefinitely

RETRY_AFTER_BODY_PATTERN = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)


class RateLimitRetryTimeout(RuntimeError):
    """Raised only when a configured finite rate-limit wait budget is exhausted."""


def _env_nonnegative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(default)


def _rate_limit_total_wait_seconds() -> float:
    return _env_nonnegative_float(
        "LLM_RATE_LIMIT_MAX_WAIT_SECONDS",
        DEFAULT_RATE_LIMIT_TOTAL_WAIT,
    )


def _retry_after_seconds(exc: Exception) -> float:
    """Best-effort wait suggestion from a RateLimitError.

    Prefers the Retry-After response header. Falls back to scraping the
    provider's "try again in 11.64s" hint out of the exception text. If
    neither is available, uses the default backoff. The result is always
    clamped to RATE_LIMIT_MAX_WAIT so a misreported hint cannot hang the
    agent for minutes.
    """

    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        for key in ("retry-after", "Retry-After"):
            raw = headers.get(key) if hasattr(headers, "get") else None
            if raw:
                try:
                    return min(float(raw), RATE_LIMIT_MAX_WAIT)
                except (TypeError, ValueError):
                    pass

    match = RETRY_AFTER_BODY_PATTERN.search(str(exc))
    if match:
        try:
            # Add 0.5s slack so we cross the reset boundary cleanly.
            return min(float(match.group(1)) + 0.5, RATE_LIMIT_MAX_WAIT)
        except ValueError:
            pass

    return float(RATE_LIMIT_RETRY_WAIT)


def _rate_limit_retry_wait(exc: Exception) -> float:
    wait = _retry_after_seconds(exc)
    if RATE_LIMIT_JITTER_MAX > 0:
        wait += random.uniform(0, RATE_LIMIT_JITTER_MAX)
    return min(wait, RATE_LIMIT_MAX_WAIT)


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


JSON_RESPONSE_FORMAT = {"type": "json_object"}
TOOL_NAMES = {"bash", "create_file", "edit_section", "replace_text"}


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
        return OpenAI(api_key=api_key, base_url=config["base_url"], max_retries=0)
    return OpenAI(api_key=api_key, max_retries=0)


def _looks_like_json_mode_rejection(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    json_mode_markers = (
        "failed_generation",
        "json_object",
        "json mode",
        "output_parse_failed",
        "parsing failed",
        "response_format",
    )
    rejection_markers = (
        "unsupported",
        "not support",
        "unrecognized",
        "unknown",
        "invalid",
        "not permitted",
        "unexpected",
        "does not accept",
        "400",
        "bad request",
    )

    return any(marker in text for marker in json_mode_markers) and any(
        marker in text for marker in rejection_markers
    )


def _json_payload(payload):
    return json.dumps(payload, ensure_ascii=False)


def _tool_call_payload(tool_name, args, reason="recovered provider tool call"):
    return _json_payload(
        {
            "type": "tool_call",
            "tool": tool_name,
            "args": args,
            "reason": reason,
        }
    )


def _error_body(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body

    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            return body

    text = str(exc)
    marker = " - "
    if marker not in text:
        return None

    raw_body = text.split(marker, 1)[1]
    try:
        parsed = ast.literal_eval(raw_body)
    except (SyntaxError, ValueError):
        return None

    if isinstance(parsed, dict):
        return parsed
    return None


def _failed_generation_json(exc):
    body = _error_body(exc)
    if not body:
        return None

    error = body.get("error")
    if error is None and "failed_generation" in body:
        error = body
    if not isinstance(error, dict):
        return None

    failed_generation = error.get("failed_generation")
    if isinstance(failed_generation, str):
        try:
            payload = json.loads(failed_generation)
        except json.JSONDecodeError:
            return _failed_generation_text_json(failed_generation)
    elif isinstance(failed_generation, dict):
        payload = failed_generation
    else:
        return None

    if isinstance(payload.get("arguments"), dict) and payload["arguments"].get("type") in {
        "final",
        "tool_call",
    }:
        return _json_payload(payload["arguments"])

    name = payload.get("name")
    if isinstance(name, str) and name.startswith("tool."):
        name = name.removeprefix("tool.")
    if name in TOOL_NAMES and isinstance(payload.get("arguments"), dict):
        return _tool_call_payload(name, payload["arguments"])
    if name == "tool_call_bash" and isinstance(payload.get("arguments"), dict):
        command = payload["arguments"].get("command")
        if isinstance(command, str) and command.strip():
            return _tool_call_payload("bash", {"command": command.strip()})

    if payload.get("type") in {"final", "tool_call"}:
        return _json_payload(payload)

    return None


def _failed_generation_text_json(text):
    path_match = re.search(r"(/[A-Za-z0-9_./-]+)", text)
    path = None
    if path_match:
        path = path_match.group(1)
        if path.endswith("."):
            path = path[:-1]

    explicit_bash = re.search(r"(?i)bash tool:\s*(.+?)(?:\n|$)", text)
    if explicit_bash:
        command = explicit_bash.group(1).strip()
        if command.endswith("."):
            command = command[:-1]
        return _tool_call_payload("bash", {"command": command})

    if path and re.search(r"(?i)\bcat\b|\bread\b|\bopen\b", text):
        return _tool_call_payload("bash", {"command": f"cat {path}"})

    replace_match = re.search(r'(?i)replace\s+"([^"]+)"\s+(?:to|with)\s+"([^"]+)"', text)
    if path and replace_match:
        return _tool_call_payload(
            "replace_text",
            {
                "path": path,
                "old_text": replace_match.group(1),
                "new_text": replace_match.group(2),
                "all_occurrences": False,
            },
        )

    return None


def _create_completion(client, model, messages, *, use_json_mode):
    kwargs = {
        "model": model,
        "messages": list(messages),
    }
    if use_json_mode:
        kwargs["response_format"] = JSON_RESPONSE_FORMAT

    return client.chat.completions.create(**kwargs)


def _create_with_rate_limit_retry(client, model, messages, *, use_json_mode, provider_name):
    attempts = 0
    waited = 0.0
    max_total_wait = _rate_limit_total_wait_seconds()

    while True:
        try:
            return _create_completion(client, model, messages, use_json_mode=use_json_mode)
        except RateLimitError as exc:
            attempts += 1
            wait = _rate_limit_retry_wait(exc)
            if max_total_wait > 0:
                remaining = max_total_wait - waited
                if remaining <= 0:
                    raise RateLimitRetryTimeout(
                        f"{provider_name} stayed rate-limited after waiting "
                        f"{waited:.1f}s (limit {max_total_wait:.1f}s)"
                    ) from exc
                wait = min(wait, remaining)
            line = (
                f"[llm] {provider_name} rate-limited "
                f"(attempt {attempts}); waiting {wait:.1f}s"
            )
            print(
                f"{colors.ts()} {colors.paint(line, colors.DIM, colors.YELLOW)}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            waited += wait


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
            response = _create_with_rate_limit_retry(
                client,
                model,
                messages,
                use_json_mode=True,
                provider_name=provider_name,
            )
        except Exception as exc:
            recovered = _failed_generation_json(exc)
            if recovered:
                return recovered

            if not _looks_like_json_mode_rejection(exc):
                errors.append(f"{provider_name}: {type(exc).__name__}: {exc}")
                continue

            try:
                response = _create_with_rate_limit_retry(
                    client,
                    model,
                    messages,
                    use_json_mode=False,
                    provider_name=provider_name,
                )
            except Exception as retry_exc:
                recovered = _failed_generation_json(retry_exc)
                if recovered:
                    return recovered

                errors.append(
                    f"{provider_name}: {type(retry_exc).__name__}: {retry_exc}"
                )
                continue

        content = response.choices[0].message.content
        return content or ""

    detail = "; ".join(errors) if errors else "no providers were tried"
    raise RuntimeError(f"I could not get a reply from any LLM provider ({detail})")
