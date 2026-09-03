"""mserver — MServerOS launcher + agent REPL."""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from . import __version__
from .agent.core import Agent
from .agent.ui import UI
from .vos.kernel import OS_NAME, OS_VERSION, VOS
from .vos.shell import Shell
from .web.server import Dashboard


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mserver",
        description=f"{OS_NAME} {OS_VERSION} — an AI agent with its own virtual Linux, built for Termux.",
    )
    ap.add_argument("--data", default=str(Path.home() / ".mserver"),
                    help="data directory (default ~/.mserver)")
    ap.add_argument("--host", default="0.0.0.0", help="dashboard bind host (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8686, help="dashboard port (default 8686)")
    ap.add_argument("--local", action="store_true", help="force offline mode (ignore API key)")
    ap.add_argument("--web", action="store_true", help="start the web dashboard alongside the REPL")
    ap.add_argument("--web-only", action="store_true", help="only run the dashboard (no REPL)")
    ap.add_argument("--version", action="version",
                    version=f"mserver {__version__}",
                    help="print the version and exit")
    ap.add_argument("--as-user", metavar="NAME", default=None,
                    help="run the agent as a non-root vOS user (e.g. --as-user "
                         "agent), so it cannot write to /etc even if it tries")
    ap.add_argument("--net", action="store_true",
                    help="allow the agent to download from the public internet "
                         "(off by default; fetched pages are untrusted input)")
    ap.add_argument("--yolo", action="store_true",
                    help="never ask before destructive actions (scripted runs)")
    ap.add_argument("--safe", action="store_true",
                    help="refuse all destructive actions outright")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    data = Path(args.data).expanduser()
    artifacts_dir = data / "presented"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    vos = VOS(data / "vos")
    shell = Shell(vos)
    ui = UI()

    def confirm(prompt: str) -> bool:
        """Ask on the terminal before a destructive tool call."""
        try:
            return input(ui.yellow(prompt)).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    if args.net:
        os.environ["MSERVER_NET"] = "1"
    if args.as_user:
        try:
            vos.users.su(args.as_user)
            ui.println(ui.dim(
                f"  running as '{args.as_user}' — permissions are enforced; "
                f"use sudo for root-only work"))
        except Exception as e:
            ui.println(ui.dim(f"  --as-user failed: {e}"))
            return 1
    gate_mode = "allow" if args.yolo else ("deny" if args.safe else "ask")
    agent = Agent(vos, shell, ui, artifacts_dir, force_local=args.local,
                  gate_mode=gate_mode,
                  confirm=None if args.web_only else confirm)
    dash = Dashboard(vos, shell, artifacts_dir, port=args.port, host=args.host,
                     agent=agent, token=os.environ.get("MSERVER_TOKEN") or None)
    agent.set_dashboard(dash)
    dash.mode_label = (
        f"AI · {agent.cfg.model}" if not agent.local else "offline · local mode"
    )

    if args.web or args.web_only:
        try:
            dash.start()
        except OSError as e:
            ui.println(ui.red(f"  could not start dashboard on {args.host}:{args.port}: {e}"))
            if args.web_only:
                return 1
        ui.println(ui.cyan(f"  dashboard → {dash.url()}"))
        ui.println(ui.cyan(f"  agent chat → {dash.chat_url()}"))
        ui.println(ui.dim("  (another device: same paths with the phone LAN IP; the token gates the chat)"))

    if args.web_only:
        ui.println(ui.dim("  press ctrl-c to stop"))
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        dash.stop()
        return 0

    ui.banner()
    if not agent.local:
        ui.println(ui.green(f"  AI mode · {agent.cfg.model} @ {agent.cfg.base_url}"))
    else:
        ui.println(ui.yellow("  offline mode — set MOPENAI_API_KEY for full AI "
                             "(any OpenAI-compatible endpoint)"))
    ui.println(ui.dim("  plain text → agent   ·   !cmd → direct msh   ·   /help → commands\n"))

    prompt = ui.cyan("mserver ❯ ")
    while True:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            ui.println()
            break
        line = line.strip()
        if not line:
            continue
        if line in ("clear", "/clear"):
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if line.startswith("/"):
            if _slash(line, agent, dash, ui):
                break
            continue
        if line.startswith("!"):
            out, err, code = shell.run(line[1:])
            ui.println(ui.dim("$ " + line[1:]))
            if out:
                ui.println(out)
            if err:
                ui.println(ui.red(err))
            continue
        answer = agent.ask(line)
        ui.panel("MSERVER AGENT", answer, color="cyan")

    if dash.running:
        dash.stop()
    shell.save_history()
    vos.scheduler.stop()   # don't leave crond ticking after the REPL exits
    ui.println(ui.dim(f"\n  {OS_NAME} halted. goodbye."))
    return 0


def _slash(line: str, agent: Agent, dash: Dashboard, ui: UI) -> bool:
    """Handle /commands. Returns True when the REPL should exit."""
    parts = line.split()
    cmd = parts[0].lower()
    if cmd in ("/exit", "/quit"):
        return True
    if cmd in ("/help", "/?"):
        ui.panel("HELP", "\n".join([
            "plain text        send to the MServer agent",
            "!ls -la /etc      run a command directly in the vOS shell",
            "/status           system report",
            "/web on|off       start/stop the dashboard",
            "/artifacts        list presented artifacts",
            "/key              LLM configuration (key stays hidden)",
            "/about            about MServerOS",
            "/clear            clear the terminal",
            "/exit             halt MServerOS",
        ]), color="cyan")
        return False
    if cmd == "/status":
        out = []
        for c in ("uptime", "ps", "df", "pkg list"):
            o, e, _ = agent.shell.run(c)
            out.append(o or e)
        ui.panel("SYSTEM STATUS", "\n\n".join(out), color="green")
        return False
    if cmd == "/web":
        arg = parts[1].lower() if len(parts) > 1 else "toggle"
        if arg in ("on", "start", "toggle") and not dash.running:
            try:
                dash.start()
                ui.println(ui.cyan(f"  dashboard → {dash.url()}"))
                ui.println(ui.cyan(f"  agent chat → {dash.chat_url()}"))
            except OSError as e:
                ui.println(ui.red(f"  dashboard failed: {e}"))
        elif arg in ("off", "stop", "toggle") and dash.running:
            dash.stop()
            ui.println(ui.dim("  dashboard stopped"))
        else:
            state = f"running at {dash.url()}" if dash.running else "stopped"
            ui.println(ui.dim(f"  dashboard {state}"))
        return False
    if cmd == "/artifacts":
        files = sorted(agent.artifacts_dir.glob("*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            ui.println(ui.dim("  nothing presented yet"))
        for f in files[:15]:
            ui.println(
                ui.dim(f"  {time_fmt(f.stat().st_mtime)}  ") + f.name
            )
        return False
    if cmd == "/key":
        cfg = agent.cfg
        if cfg.has_key:
            ui.println(ui.green(f"  API key: set ({cfg.api_key[:4]}…{cfg.api_key[-4:]})"))
        else:
            ui.println(ui.yellow("  API key: not set (offline mode)"))
        ui.println(ui.dim(f"  model: {cfg.model}"))
        ui.println(ui.dim(f"  endpoint: {cfg.base_url}"))
        return False
    if cmd == "/about":
        ui.panel("ABOUT",
                 f"{OS_NAME} {OS_VERSION} · mserver {__version__}\n"
                 f"An AI agent with its own virtual Linux OS, sandboxed to:\n"
                 f"  {agent.vos.root}\n\n"
                 f"Built for Termux on Android. The agent never touches the real "
                 f"device filesystem.",
                 color="cyan")
        return False
    ui.println(ui.red(f"  unknown command: {cmd} (try /help)"))
    return False


def time_fmt(ts: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))


if __name__ == "__main__":
    sys.exit(main())
