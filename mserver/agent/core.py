"""The agent loop: LLM with tools, plus an offline local mode."""
from __future__ import annotations

import json
import re

from . import llm
from .tools import build_tools, fmt_shell
from .ui import UI
from ..vos.kernel import OS_NAME, OS_VERSION

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


class Agent:
    def __init__(self, vos, shell, ui: UI, artifacts_dir, force_local: bool = False):
        self.vos = vos
        self.shell = shell
        self.ui = ui
        self.artifacts_dir = artifacts_dir
        self.cfg = llm.Config.from_env()
        self.local = force_local or not self.cfg.has_key
        self._dashboard = None

        def on_present(title, content, fname):
            path = self.artifacts_dir / fname
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
            self.ui.panel(f"PRESENT · {title}", content, color="green")

        self.schemas, self.executors = build_tools(
            vos, shell,
            {"on_present": on_present, "dashboard": self._dashboard_hook},
        )
        self.messages = [{"role": "system", "content": self._system_prompt()}]

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
- When the task is finished, call `present` exactly once with the final deliverable (a report, a config file, a table, ASCII art), then reply with a 1-3 line summary.
- Be direct, technical and brief. No filler."""

    # ----------------------------------------------------------------- ask
    def ask(self, text: str) -> str:
        if self.local:
            return self._local(text)
        self.messages.append({"role": "user", "content": text})
        for _ in range(MAX_STEPS):
            try:
                resp = llm.chat(self.messages, self.schemas, self.cfg)
            except llm.LLMError as e:
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
                fn = self.executors.get(c["name"])
                if fn is None:
                    result = f"error: unknown tool {c['name']}"
                else:
                    try:
                        result = fn(args)
                    except Exception as e:
                        result = f"error: {e}"
                self.messages.append(
                    {"role": "tool", "tool_call_id": c["id"], "content": str(result)}
                )
        return "(stopped: reached the tool-call limit — say 'continue' to go on)"

    def _parse_args(self, raw: str) -> dict:
        try:
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # -------------------------------------------------------- offline mode
    def _local(self, text: str) -> str:
        t = text.strip()
        low = t.lower()
        first = low.split()[0] if low.split() else ""
        if first in self.shell.commands:
            out, err, code = self.shell.run(t)
            return fmt_shell(t, out, err, code)
        m = re.match(r"install\s+([a-z0-9._-]+)", low)
        if m:
            out, err, code = self.shell.run(f"pkg install {m.group(1)}")
            return fmt_shell(f"pkg install {m.group(1)}", out, err, code)
        m = re.match(r"(?:remove|uninstall)\s+([a-z0-9._-]+)", low)
        if m:
            out, err, code = self.shell.run(f"pkg remove {m.group(1)}")
            return fmt_shell(f"pkg remove {m.group(1)}", out, err, code)
        m = re.match(r"(?:show|read|open|cat)\s+(/\S+)", low)
        if m:
            out, err, code = self.shell.run(f"cat {m.group(1)}")
            return fmt_shell(f"cat {m.group(1)}", out, err, code)
        if low in ("status", "system status", "system report", "report"):
            up, _, _ = self.shell.run("uptime")
            ps, _, _ = self.shell.run("ps")
            df, _, _ = self.shell.run("df")
            pk, _, _ = self.shell.run("pkg list")
            return f"{up}\n\n{ps}\n\n{df}\n\n{pk}"
        if low in ("neofetch", "about", "who are you"):
            out, _, _ = self.shell.run("neofetch")
            return out
        if low in ("list files", "list", "ls"):
            out, _, _ = self.shell.run("ls -la /")
            return out
        if low in ("help", "what can you do"):
            return _LOCAL_HELP
        if low in ("reboot", "restart the os"):
            out, _, _ = self.shell.run("reboot")
            return out
        return (
            f"Offline mode (no LLM API key), so I only run direct vOS commands.\n"
            f'I couldn\'t map "{t}" to one.\n\n'
            "Try: 'status', 'neofetch', 'install nginx', 'show /etc/hosts', 'ls -la /'\n"
            "Or set MOPENAI_API_KEY for full AI mode (see /help in the README)."
        )
