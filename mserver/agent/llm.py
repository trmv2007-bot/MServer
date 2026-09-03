"""Minimal OpenAI-compatible chat client (stdlib only — Termux friendly)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 180
DEFAULT_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0


class LLMError(Exception):
    pass


class LLMRetryable(LLMError):
    """A failure worth trying again: rate limit, 5xx, or a network blip."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(err) -> float | None:
    """Parse a Retry-After header, if the endpoint sent a usable one."""
    try:
        value = err.headers.get("Retry-After")
    except Exception:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _env_num(name: str, default, cast):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


@dataclass
class Config:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES

    @classmethod
    def from_env(cls, data_dir=None) -> Config:
        """Build config from stored settings, overridden by the environment.

        Precedence is environment > stored file > default, so exporting
        MOPENAI_API_KEY keeps working exactly as before and a stored file
        cannot silently reconfigure a deployment that sets its own vars.
        """
        stored = {}
        if data_dir is not None:
            try:
                from . import settings
                stored = settings.load(data_dir)
            except Exception:
                stored = {}

        def pick(field, env_names, default, cast=None):
            for n in env_names:
                raw = os.environ.get(n)
                if raw:
                    if cast is None:
                        return raw
                    try:
                        return cast(raw)
                    except (TypeError, ValueError):
                        return default
            if field in stored and stored[field] not in (None, ""):
                try:
                    return cast(stored[field]) if cast else stored[field]
                except (TypeError, ValueError):
                    return default
            return default

        return cls(
            api_key=pick("api_key", ("MOPENAI_API_KEY", "OPENAI_API_KEY"), ""),
            base_url=pick("base_url", ("MOPENAI_BASE_URL",), DEFAULT_BASE_URL),
            model=pick("model", ("MOPENAI_MODEL",), DEFAULT_MODEL),
            timeout=pick("timeout", ("MOPENAI_TIMEOUT",), DEFAULT_TIMEOUT, float),
            retries=pick("retries", ("MOPENAI_RETRIES",), DEFAULT_RETRIES, int),
        )

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def chat(messages: list, tools: list | None, cfg: Config,
         retries: int | None = None) -> dict:
    """One chat-completions round trip, retrying transient failures.

    Returns {"content": str|None, "tool_calls": [{"id","name","arguments_raw"}]}.
    Works with any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq,
    LM Studio, Ollama at http://localhost:11434/v1, ...).

    Rate limits (429) and server errors (5xx) are retried with exponential
    backoff, honouring Retry-After when the endpoint sends it. A phone on
    mobile data drops connections routinely, so transient network errors are
    retried too. Client errors (4xx other than 429) are not retried — they
    will fail identically every time.
    """
    attempts = (cfg.retries if retries is None else retries) + 1
    delay = RETRY_BASE_DELAY
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return _chat_once(messages, tools, cfg)
        except LLMRetryable as e:
            last = e
            if attempt == attempts - 1:
                break
            wait = e.retry_after if e.retry_after is not None else delay
            time.sleep(min(wait, RETRY_MAX_DELAY))
            delay = min(delay * 2, RETRY_MAX_DELAY)
    raise LLMError(f"{last} (after {attempts} attempts)") from last


def _chat_once(messages: list, tools: list | None, cfg: Config) -> dict:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body: dict = {"model": cfg.model, "messages": messages, "temperature": 0.4}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        msg = f"HTTP {e.code} from endpoint: {detail}"
        if e.code == 429 or e.code >= 500:
            raise LLMRetryable(msg, retry_after=_retry_after(e)) from e
        raise LLMError(msg) from e
    except urllib.error.URLError as e:
        # DNS failure, connection reset, flaky mobile data — worth retrying.
        raise LLMRetryable(f"cannot reach {cfg.base_url}: {e.reason}") from e
    except (TimeoutError, json.JSONDecodeError, OSError) as e:
        raise LLMRetryable(f"LLM request failed: {e}") from e
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("unexpected LLM response shape") from e
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        calls.append({
            "id": tc.get("id") or f"call_{len(calls)}",
            "name": fn.get("name") or "",
            "arguments_raw": fn.get("arguments") or "{}",
        })
    return {"content": msg.get("content"), "tool_calls": calls}
