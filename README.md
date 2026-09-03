# MServer

**An AI agent with its own virtual Linux OS — built to run on Termux (Android).**

MServer boots **MServerOS**: a small, sandboxed, Linux-like operating environment
with its own filesystem, shell (`msh`), package manager, services and process
table — all living inside one data directory on your phone. On top of it sits
an **AI agent** you talk to in plain English. It installs packages, writes
files, runs commands, manages services and **presents** results back to you in
the terminal — and on the web dashboard if you open it.

```
mserver ❯ install nginx, make a welcome page, start it and show me the site
  ⚙ vos_run command=pkg install nginx
  ⚙ vos_write path=/srv/www/index.html content=<!doctype html>...
  ⚙ vos_run command=service nginx start
  ⚙ present title=nginx site config content=...
╭─ PRESENT · nginx site config ───────────────────────────╮
│ listen 80;  root /srv/www;                               │
│ service nginx → running (pid 13)                         │
╰──────────────────────────────────────────────────────────╯
```

## What you get

| Part | What it is |
| --- | --- |
| **vOS** (`mserver/vos`) | The "inbuilt Linux": a sandboxed rootfs with an `msh` shell (pipes, redirection, globs), `pkg` manager, `service` manager, process table, `neofetch`, `reboot`, … |
| **Agent** (`mserver/agent`) | LLM-driven agent with tools (`vos_run`, `vos_read/write`, `pkg_install`, `present`, …). Works with any OpenAI-compatible endpoint. Has an offline mode when no key is set. |
| **Presentation** | Finished work is shown in a highlighted terminal panel, saved as an artifact (`.mserver/presented/*.md`), and listed on the dashboard. |
| **Dashboard** (`mserver/web`) | Zero-dependency web UI: system info, processes, storage, packages, recent commands, artifacts. Auto-refreshes. |

No pip packages. Pure Python 3 stdlib — everything runs from `pkg install python`.

## Quick start (Termux)

```sh
pkg install python git
git clone https://github.com/trmv2007-bot/MServer.git && cd MServer
bash termux-setup.sh     # optional convenience (pkg update + install python)
bash run.sh
```

Or simply `bash run.sh` if you already have `python3`.

## Using the agent

Plain text goes to the agent; `!command` runs directly in the vOS shell;
`/help` lists session commands.

```
mserver ❯ status
mserver ❯ neofetch
mserver ❯ install nginx and start it
mserver ❯ write a README for /opt/myapp and present it
mserver ❯ what packages can I install?
mserver ❯ reboot the system and show me the processes
mserver ❯ !ls -la /etc
```

### AI mode (full agent)

Set an API key before starting — works with **any OpenAI-compatible endpoint**
(OpenAI, OpenRouter, Groq, LM Studio, Ollama, …):

| Variable | Default | Purpose |
| --- | --- | --- |
| `MOPENAI_API_KEY` | — | API key (or `OPENAI_API_KEY`) |
| `MOPENAI_BASE_URL` | `https://api.openai.com/v1` | endpoint base URL |
| `MOPENAI_MODEL` | `gpt-4o-mini` | model name |

```sh
export MOPENAI_API_KEY=sk-...
export MOPENAI_BASE_URL=https://openrouter.ai/api/v1    # example
export MOPENAI_MODEL=openai/gpt-4o-mini
bash run.sh
```

### Offline mode (no key)

Without a key (or with `--local`), the agent runs a built-in local planner:
`status`, `neofetch`, `install <pkg>`, `show /path`, `ls -la /`, `reboot`, …
still work, and `!` direct shell always does.

## The vOS

- **Sandbox guarantee**: the whole vOS lives under the data dir
  (`~/.mserver/vos` by default). Path traversal is rejected, no command ever
  reaches the real Android shell, and the device filesystem is never touched.
- **Shell**: `ls cd pwd cat echo mkdir touch rm cp mv grep head tail wc find
  env ps kill reboot clear history which free df neofetch date uptime uname
  whoami hostname pkg service help` + pipes `|`, redirection `>` `>>`, globs `* ? [`.
- **Packages** (`pkg list`): `hello`, `cowsay`, `figlet`, `nginx`, `git`, `ssh`.
  Installing drops real files into the vOS and registers shell commands;
  the install list persists across restarts.
- **Services** (`service ssh start`, `service nginx stop`): show up in `ps`,
  survive until `reboot`.

## Web dashboard

```sh
bash run.sh --web        # dashboard + REPL
bash run.sh --web-only   # dashboard only (handy for Termux: open it, keep it running)
```

Open `http://localhost:8686` in the phone browser — or
`http://<phone-lan-ip>:8686` from another device
(find the IP with `ip route get 1` or in Android settings). The agent can also
start/stop the dashboard itself via its `dashboard` tool.

## Project layout

```
MServer/
├── run.sh                 # launcher (python3 -m mserver)
├── termux-setup.sh        # Termux one-shot setup
├── mserver/
│   ├── main.py            # CLI, REPL, slash commands
│   ├── vos/
│   │   ├── kernel.py      # sandboxed rootfs, processes, services
│   │   ├── shell.py       # msh interpreter
│   │   └── packages.py    # pkg registry (cowsay, nginx, git, …)
│   ├── agent/
│   │   ├── core.py        # agent loop + offline planner
│   │   ├── tools.py       # the agent's hands (all sandboxed)
│   │   ├── llm.py         # OpenAI-compatible client (stdlib urllib)
│   │   └── ui.py          # ANSI panels, banner, tool trace
│   └── web/server.py      # dashboard (http.server, thread)
└── tests/                 # dependency-free tests
```

## Extending

- **New vOS package**: add a `Package(...)` (files + optional shell commands)
  in `mserver/vos/packages.py` and list it in `build_registry()`.
- **New agent tool**: add an executor + schema in `mserver/agent/tools.py` —
  the agent picks it up automatically.

## Safety notes

- The agent's reach is exactly the vOS sandbox: `vos_write`, `vos_delete`,
  `vos_run` are all path-confined, and package names are regex-checked.
- The LLM key only leaves the phone to the endpoint you configure.
- `--data DIR` moves the sandbox anywhere you like.

## Tests

```sh
python3 tests/test_vos.py
python3 tests/test_tools.py
```
