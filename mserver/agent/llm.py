"""Minimal OpenAI-compatible chat client (stdlib only — Termux friendly)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(Exception):
    pass


@dataclass
class Config:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_key=os.environ.get("MOPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
            base_url=os.environ.get("MOPENAI_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("MOPENAI_MODEL") or DEFAULT_MODEL,
        )

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def chat(messages: list, tools: list | None, cfg: Config) -> dict:
    """One chat-completions round trip.

    Returns {"content": str|None, "tool_calls": [{"id","name","arguments_raw"}]}.
    Works with any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq,
    LM Studio, Ollama at http://localhost:11434/v1, ...).
    """
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
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        raise LLMError(f"HTTP {e.code} from endpoint: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {cfg.base_url}: {e.reason}") from e
    except (TimeoutError, json.JSONDecodeError, OSError) as e:
        raise LLMError(f"LLM request failed: {e}") from e
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
