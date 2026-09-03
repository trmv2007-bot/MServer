"""Persisted LLM settings, editable from the dashboard.

Configuration used to be environment variables only, which meant "add your
API key" was answered with "edit ~/.bashrc and restart" — a poor answer when
the product already has a web UI in front of you.

Where the file lives, and why it matters
----------------------------------------
`~/.mserver/config.json`, i.e. the data directory — **not** inside the vOS
rootfs (`~/.mserver/vos`). This is a security boundary, not a filing
preference. The agent has full read access to its own filesystem: if the key
were stored in the rootfs the model could `cat` its own credentials, include
them in a reply, or write them into a file a prompt-injected page asked it to
create. Outside the rootfs, `vpath()` makes that impossible.

The file is written `chmod 600`. Environment variables still win, so an
existing deployment that exports `MOPENAI_API_KEY` keeps behaving exactly as
before and a CI runner cannot be silently reconfigured by a stored file.
"""
from __future__ import annotations

import json
import os

FILENAME = "config.json"

# Only these keys are accepted from a settings form. An allow-list rather
# than "write whatever JSON arrives" — the file is read at startup and must
# not become a way to set arbitrary attributes.
FIELDS = ("api_key", "base_url", "model", "timeout", "retries")

# Environment variables take precedence over the stored file.
ENV_MAP = {
    "api_key": ("MOPENAI_API_KEY", "OPENAI_API_KEY"),
    "base_url": ("MOPENAI_BASE_URL",),
    "model": ("MOPENAI_MODEL",),
    "timeout": ("MOPENAI_TIMEOUT",),
    "retries": ("MOPENAI_RETRIES",),
}


class SettingsError(Exception):
    pass


def config_path(data_dir) -> object:
    from pathlib import Path
    return Path(data_dir) / FILENAME


def load(data_dir) -> dict:
    """Read stored settings. Never raises — a corrupt file means defaults."""
    try:
        p = config_path(data_dir)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in FIELDS}
    except Exception:
        return {}


def save(data_dir, values: dict) -> dict:
    """Merge and persist settings, 0600. Returns the stored dict."""
    from pathlib import Path
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    current = load(d)
    for k, v in (values or {}).items():
        if k not in FIELDS:
            continue
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    p = config_path(d)
    # Create with restrictive permissions from the outset rather than
    # chmod-ing afterwards, which would leave a window where the key is
    # world-readable.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception as e:
        raise SettingsError(f"could not write settings: {e}") from e
    try:
        os.chmod(str(p), 0o600)          # in case the file already existed
    except OSError:
        pass
    return current


def clear(data_dir) -> None:
    try:
        config_path(data_dir).unlink()
    except (OSError, AttributeError):
        pass


def env_overrides() -> dict:
    """Which fields are pinned by the environment, and so not editable."""
    out = {}
    for field, names in ENV_MAP.items():
        for n in names:
            if os.environ.get(n):
                out[field] = n
                break
    return out


def validate(values: dict) -> dict:
    """Check and coerce a settings payload from the web form."""
    out = {}
    if "api_key" in values:
        key = str(values["api_key"] or "").strip()
        if key:
            if len(key) < 8 or any(c.isspace() for c in key):
                raise SettingsError(
                    "that does not look like an API key (too short, or it "
                    "contains spaces — check for a stray copy/paste)")
            out["api_key"] = key
        else:
            out["api_key"] = None          # explicit clear
    if "base_url" in values:
        url = str(values["base_url"] or "").strip().rstrip("/")
        if url:
            if not url.startswith(("http://", "https://")):
                raise SettingsError("endpoint must start with http:// or https://")
            if url.startswith("http://") and not _is_local(url):
                # Plain HTTP to a remote host would put the key on the wire
                # in clear text. Local endpoints (Ollama, llama.cpp) are the
                # legitimate case and are allowed.
                raise SettingsError(
                    "refusing to send your API key over plain http:// to a "
                    "remote host — use https://, or a localhost endpoint")
            out["base_url"] = url
        else:
            out["base_url"] = None
    if "model" in values:
        model = str(values["model"] or "").strip()
        out["model"] = model or None
    if "timeout" in values and str(values["timeout"]).strip():
        try:
            t = float(values["timeout"])
        except (TypeError, ValueError):
            raise SettingsError("timeout must be a number of seconds") from None
        if not 1 <= t <= 3600:
            raise SettingsError("timeout must be between 1 and 3600 seconds")
        out["timeout"] = t
    if "retries" in values and str(values["retries"]).strip():
        try:
            r = int(values["retries"])
        except (TypeError, ValueError):
            raise SettingsError("retries must be a whole number") from None
        if not 0 <= r <= 10:
            raise SettingsError("retries must be between 0 and 10")
        out["retries"] = r
    return out


def _is_local(url: str) -> bool:
    host = url.split("://", 1)[1].split("/")[0].split(":")[0].lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def mask(key: str) -> str:
    """Show enough to recognise a key, never enough to use it."""
    if not key:
        return ""
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 6}{key[-4:]}"
