"""Context window management for the agent loop.

Without this the conversation grows without bound: every turn re-sends the
whole history, so input cost grows quadratically and long sessions eventually
get rejected by the endpoint outright.

Three deterministic passes, cheapest first — none of them call the model:

1. ``dedupe_tool_results``  — a repeated (tool, args) pair keeps only its most
   recent output; older copies become a short pointer.
2. ``purge_resolved_errors`` — an error whose tool later succeeded with the
   same arguments is no longer useful, so it is collapsed.
3. ``mask_observations``   — older long tool outputs are replaced by a
   ``[output masked]`` placeholder. The action and the model's reasoning are
   preserved; only the wall of text goes.

Masking is the safest, lightest-touch form of compaction: it keeps the shape
of the conversation intact and costs nothing to compute.

Hard invariant
--------------
Every ``tool`` message must keep the ``tool_call_id`` that pairs it with the
assistant ``tool_calls`` entry that spawned it. OpenAI-compatible endpoints
reject a history where that pairing is broken. So these passes only ever
*rewrite message content* — they never delete a message and never reorder one.
The system prompt (index 0) is never touched, which also keeps the prompt
prefix byte-stable for provider-side prompt caching.
"""
from __future__ import annotations

# Rough token estimate. Good enough for budgeting; avoids a tokenizer dep,
# which matters because MServer is pure stdlib.
CHARS_PER_TOKEN = 4

# Default context budget (tokens). Deliberately conservative: MServer targets
# any OpenAI-compatible endpoint, including small local models via Ollama or
# LM Studio, where an 8k window is common.
DEFAULT_MAX_TOKENS = 8000

# Compact at 80% rather than 100%, leaving headroom for the next tool result
# and the model's reply.
TRIGGER_RATIO = 0.8

# Tool outputs shorter than this are cheap enough to keep verbatim.
MASK_THRESHOLD = 400

# Most recent tool results always kept at full fidelity.
KEEP_RECENT = 6


def estimate_tokens(messages: list) -> int:
    """Approximate token count for a message list."""
    return sum(_msg_chars(m) for m in messages) // CHARS_PER_TOKEN


def _msg_chars(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    return n


def _call_index(messages: list) -> dict:
    """Map tool_call_id -> (tool name, raw arguments)."""
    index: dict[str, tuple[str, str]] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            index[tc.get("id")] = (fn.get("name") or "", fn.get("arguments") or "")
    return index


def _is_masked(content: str) -> bool:
    return content.startswith("[") and content.endswith("]") and "…" not in content[:1]


def dedupe_tool_results(messages: list) -> tuple[list, int]:
    """Collapse repeated identical (tool, args) results, keeping the newest.

    Re-reading the same file five times should not cost five copies of it.
    """
    index = _call_index(messages)
    # Walk backwards so the *most recent* occurrence is the one kept.
    seen: set[tuple[str, str]] = set()
    out = list(messages)
    n = 0
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") != "tool":
            continue
        key = index.get(m.get("tool_call_id"))
        if key is None or not key[0]:
            continue
        if key in seen:
            content = str(m.get("content") or "")
            if len(content) > 80 and not content.startswith("[duplicate of"):
                out[i] = dict(m, content=f"[duplicate of a later {key[0]} call, omitted]")
                n += 1
        else:
            seen.add(key)
    return out, n


def purge_resolved_errors(messages: list) -> tuple[list, int]:
    """Collapse errors that a later identical call resolved.

    If ``vos_run pkg install nginx`` failed once and then succeeded with the
    same arguments, the failure is noise.
    """
    index = _call_index(messages)
    resolved: set[tuple[str, str]] = set()
    out = list(messages)
    n = 0
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") != "tool":
            continue
        key = index.get(m.get("tool_call_id"))
        if key is None or not key[0]:
            continue
        content = str(m.get("content") or "")
        failed = content.startswith("error:") or "[stderr]" in content or "[exit " in content
        if not failed:
            resolved.add(key)
        elif key in resolved and not content.startswith("[earlier "):
            out[i] = dict(m, content=f"[earlier {key[0]} error, resolved by a later call]")
            n += 1
    return out, n


def mask_observations(messages: list, keep_recent: int = KEEP_RECENT,
                      threshold: int = MASK_THRESHOLD) -> tuple[list, int]:
    """Replace older long tool outputs with a placeholder."""
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = set(tool_positions[-keep_recent:]) if keep_recent else set()
    out = list(messages)
    n = 0
    for i in tool_positions:
        if i in protected:
            continue
        content = str(out[i].get("content") or "")
        if len(content) <= threshold or content.startswith("[output:"):
            continue
        approx = len(content) // CHARS_PER_TOKEN
        head = content[:120].replace("\n", " ")
        out[i] = dict(out[i],
                      content=f"[output: ~{approx} tokens, masked to save context] {head}…")
        n += 1
    return out, n


def compact(messages: list, max_tokens: int = DEFAULT_MAX_TOKENS,
            trigger_ratio: float = TRIGGER_RATIO) -> tuple[list, dict]:
    """Compact ``messages`` if they exceed ``trigger_ratio`` of the budget.

    Returns ``(messages, report)``. The report is always returned, with
    ``compacted=False`` when nothing needed doing.
    """
    before = estimate_tokens(messages)
    trigger = int(max_tokens * trigger_ratio)
    report = {"compacted": False, "before": before, "after": before, "trigger": trigger,
              "deduped": 0, "purged": 0, "masked": 0}
    if before <= trigger:
        return messages, report

    msgs, report["deduped"] = dedupe_tool_results(messages)
    msgs, report["purged"] = purge_resolved_errors(msgs)
    if estimate_tokens(msgs) > trigger:
        msgs, report["masked"] = mask_observations(msgs)

    # Still over budget after the free passes: mask harder, keeping fewer
    # recent observations verbatim rather than dropping messages (which would
    # break tool_call_id pairing).
    if estimate_tokens(msgs) > trigger:
        msgs, extra = mask_observations(msgs, keep_recent=2, threshold=200)
        report["masked"] += extra

    report["after"] = estimate_tokens(msgs)
    report["compacted"] = True
    return msgs, report


def format_report(report: dict) -> str:
    """One-line human summary, for the tool trace / logs."""
    if not report.get("compacted"):
        return ""
    saved = report["before"] - report["after"]
    bits = []
    if report["deduped"]:
        bits.append(f"{report['deduped']} duplicate")
    if report["purged"]:
        bits.append(f"{report['purged']} resolved-error")
    if report["masked"]:
        bits.append(f"{report['masked']} masked")
    detail = ", ".join(bits) or "no-op"
    return (f"context compacted: {report['before']}→{report['after']} tokens "
            f"(-{saved}); {detail}")
