"""The agent loop: LLM with tools, plus an offline local mode."""
from __future__ import annotations

import json
import os
import re
import threading

from ..vos.kernel import OS_NAME, OS_VERSION
from . import context, llm, risk
from .audit import AuditLog, LoopGuard
from .tools import build_tools, fmt_shell
from .ui import UI

MAX_STEPS = 12

_LOCAL_HELP = """What I can do:

  vOS control   install packages, start/stop services, run any msh command,
                write files, search the filesystem, reboot the system
  presenting    when a task is done I present the result (report, config,
                art) in a panel and save it as an artifact
  dashboard     I can start/stop the web dashboard so you can watch the vOS

Offline examples (work without an API key):
  status · neofetch · ls -la / · install nginx · show /etc/hosts · reboot

For full AI mode set an API key:
  export MOPENAI_API_KEY=sk-...
  export MOPENAI_BASE_URL=https://api.openai.com/v1   # or any compatible endpoint
  export MOPENAI_MODEL=gpt-4o-mini
then restart mserver."""


def _env_int(name: str, default: int) -> int:
    try:
        return max(1000, int(os.environ.get(name, "")))
    except ValueError:
        return default


class Agent:
    def __init__(self, vos, shell, ui: UI, artifacts_dir, force_local: bool = False,
                 gate_mode: str = "ask", confirm=None):
        self.vos = vos
        self.shell = shell
        self.ui = ui
        self.artifacts_dir = artifacts_dir
        self.cfg = llm.Config.from_env()
        self.local = force_local or not self.cfg.has_key
        self._dashboard = None
        self.audit = AuditLog(vos)
        self.loop_guard = LoopGuard()
        self.gate = risk.Gate(
            mode=gate_mode,
            confirm=confirm,
            on_snapshot=lambda label: shell.snapshots.save(label=label),
        )
        self.max_context_tokens = _env_int("MSERVER_MAX_CONTEXT",
                                           context.DEFAULT_MAX_TOKENS)

        def on_present(title, content, fname):
            path = self.artifacts_dir / fname
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
            self.ui.panel(f"PRESENT · {title}", content, color="green")

        self.schemas, self.executors = build_tools(
            vos, shell,
            {"on_present": on_present, "dashboard": self._dashboard_hook},
        )
        self._lock = threading.Lock()  # serializes REPL + web-chat turns
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        self.audit.note(
            f"session start · {'offline' if self.local else self.cfg.model}")

    # -------------------------------------------------------------- wiring
    def set_dashboard(self, dash) -> None:
        self._dashboard = dash

    def _dashboard_hook(self, action: str, port: int) -> str:
        d = self._dashboard
        if d is None:
            return "dashboard is not available in this session"
        if action == "start":
            d.port = port
            d.start()
            return f"dashboard running at {d.url()}"
        if action == "stop":
            d.stop()
            return "dashboard stopped"
        return f"dashboard {'running at ' + d.url() if d.running else 'stopped'}"

    def _system_prompt(self) -> str:
        installed = ", ".join(self.vos.installed_packages()) or "none"
        return f"""You are MServer Agent, the AI core of {OS_NAME} {OS_VERSION}, a virtual Linux-like OS running inside Termux on the user's Android phone.

Ground rules:
- The vOS filesystem is fully sandboxed: every path you touch lives inside the vOS data directory, never on the real phone.
- You act only through tools. Prefer `vos_run` for anything shell-like: msh supports ls, cat, echo, mkdir, touch, rm, cp, mv, grep, head, tail, wc, find, env, ps, kill, uptime, df, free, pkg, service, neofetch, reboot, plus commands from installed packages. Pipes (|), redirection (>, >>) and globs work.
- Installed packages right now: {installed}.
- To change part of an existing file use `vos_edit` (replace an exact fragment), not `vos_write`. Only use `vos_write` to create a file or replace it wholesale.
- Before destructive or risky work, take a `snapshot_save` first. The whole filesystem can then be restored with `snapshot_rollback`.
- Destructive actions (deleting files, `rm`, rolling back) may be refused or require the user's confirmation. If a call comes back refused, do not retry it — explain what you wanted to do and why, and ask the user.
- When the task is finished, call `present` exactly once with the final deliverable (a report, a config file, a table, ASCII art), then reply with a 1-3 line summary.
- Be direct, technical and brief. No filler."""

    # ----------------------------------------------------------------- ask
    def ask(self, text: str, events: list | None = None) -> str:
        """One agent turn. `events` (optional list) receives the tool calls
        made during this turn — used by the web chat to render progress."""
        with self._lock:
            if events is None:
                events = []
            if self.local:
                return self._local(text, events)
            self.messages.append({"role": "user", "content": text})
            for _ in range(MAX_STEPS):
                self._compact()
                try:
                    resp = llm.chat(self.messages, self.schemas, self.cfg)
                except llm.LLMError as e:
                    self.audit.note(f"LLM error: {e}", source="llm")
                    self.messages.append({"role": "assistant", "content": "LLM call failed."})
                    return (
                        f"⚠ LLM error: {e}\n\n"
                        f"Falling back to offline answers. Set MOPENAI_API_KEY "
                        f"(any OpenAI-compatible endpoint) for full AI mode."
                    )
                calls = resp["tool_calls"]
                if not calls:
                    content = (resp["content"] or "").strip() or "(empty reply)"
                    self.messages.append({"role": "assistant", "content": content})
                    return content
                self.messages.append({
                    "role": "assistant",
                    "content": resp["content"],
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments_raw"]}}
                        for c in calls
                    ],
                })
                for c in calls:
                    args = self._parse_args(c["arguments_raw"])
                    self.ui.tool_trace(c["name"], args)
                    events.append({
                        "name": c["name"],
                        "args": {k: (str(v)[:200] + "…" if len(str(v)) > 200 else str(v))
                                 for k, v in args.items()},
                    })
                    fn = self.executors.get(c["name"])
                    ok = True
                    allowed, reason = self.gate.check(c["name"], args)
                    if not allowed:
                        # Deterministic refusal — the model does not get to
                        # argue its way past this.
                        result = reason
                        ok = False
                        self.audit.log(c["name"], args, reason, ok=False,
                                       source="gate")
                        self.ui.println(self.ui.yellow(f"  ⛔ {reason}"))
                    elif fn is None:
                        result = f"error: unknown tool {c['name']}"
                        ok = False
                        self.audit.log(c["name"], args, str(result), ok=ok)
                    else:
                        try:
                            result = fn(args)
                        except Exception as e:
                            result = f"error: {e}"
                            ok = False
                        self.audit.log(c["name"], args, str(result), ok=ok)

                    # Nudge the model out of a repeated identical call.
                    warning = self.loop_guard.check(c["name"], args)
                    if warning:
                        self.audit.note(f"loop guard: {c['name']} repeated",
                                        source="guard")
                        result = f"{result}\n\n{warning}"

                    self.messages.append(
                        {"role": "tool", "tool_call_id": c["id"], "content": str(result)}
                    )
            return "(stopped: reached the tool-call limit — say 'continue' to go on)"

    def _compact(self) -> dict:
        """Keep the conversation inside its token budget.

        Without this `self.messages` grows unbounded: cost rises quadratically
        and the endpoint eventually rejects the request outright.
        """
        self.messages, report = context.compact(
            self.messages, max_tokens=self.max_context_tokens)
        if report["compacted"]:
            line = context.format_report(report)
            self.audit.note(line, source="context")
            self.ui.println(self.ui.dim(f"  ⋯ {line}"))
        return report

    def context_tokens(self) -> int:
        """Approximate size of the live conversation, for /status and tests."""
        return context.estimate_tokens(self.messages)

    def reset(self) -> str:
        """Drop the conversation, keeping the system prompt."""
        n = len(self.messages) - 1
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        self.loop_guard.reset()
        self.audit.note(f"conversation reset ({n} messages dropped)")
        return f"conversation reset ({n} messages dropped)"

    def _parse_args(self, raw: str) -> dict:
        try:
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # -------------------------------------------------------- offline mode
    def _local(self, text: str, events: list | None = None) -> str:
        events = events if events is not None else []

        def run(cmd: str) -> str:
            out, err, code = self.shell.run(cmd)
            events.append({"name": cmd.split()[0], "args": {"command": cmd}})
            self.audit.log("vos_run", {"command": cmd}, out or err,
                           ok=(code == 0), source="offline")
            return fmt_shell(cmd, out, err, code)

        t = text.strip()
        low = t.lower()
        first = low.split()[0] if low.split() else ""
        if first in self.shell.commands:
            return run(t)
        m = re.match(r"install\s+([a-z0-9._-]+)", low)
        if m:
            return run(f"pkg install {m.group(1)}")
        m = re.match(r"(?:remove|uninstall)\s+([a-z0-9._-]+)", low)
        if m:
            return run(f"pkg remove {m.group(1)}")
        m = re.match(r"(?:show|read|open|cat)\s+(/\S+)", low)
        if m:
            return run(f"cat {m.group(1)}")
        if low in ("status", "system status", "system report", "report"):
            up = run("uptime")
            ps = run("ps")
            df = run("df")
            pk = run("pkg list")
            return f"{up}\n\n{ps}\n\n{df}\n\n{pk}"
        if low in ("neofetch", "about", "who are you"):
            return run("neofetch")
        if low in ("list files", "list", "ls"):
            return run("ls -la /")
        if low in ("help", "what can you do"):
            return _LOCAL_HELP
        if low in ("reboot", "restart the os"):
            return run("reboot")
        return (
            f"Offline mode (no LLM API key), so I only run direct vOS commands.\n"
            f'I couldn\'t map "{t}" to one.\n\n'
            "Try: 'status', 'neofetch', 'install nginx', 'show /etc/hosts', 'ls -la /'\n"
            "Or set MOPENAI_API_KEY for full AI mode (see /help in the README)."
        )
