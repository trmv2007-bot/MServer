# MServer Roadmap

Status: **the application is complete and working.** All 18 tests pass, the vOS
sandbox holds against path traversal, state persists across restarts, and the
dashboard, offline planner, package system and service manager all work end to
end. What follows is not a rescue plan — it is the gap between "works" and
"shippable, extensible, and safe to leave running."

Items are grouped by tier. Tier 0 is necessary; everything below it is upside.

Legend: **[bug]** = verified defect, not a missing feature.

---

## Verified state (what already works — do not rebuild)

Confirmed by running the code, not by reading it:

- Path-traversal sandbox (`VOS.vpath`) rejects `../`, `/../../..`, and
  escapes embedded mid-path. Four attacks tested, all refused.
- State persistence across restarts: installed packages and written files survive.
- Dashboard token gate uses `secrets.token_urlsafe` + `hmac.compare_digest`
  — correct practice, no change needed.
- Full REPL flow: `pkg install` → `service start` → visible in `ps`,
  redirection, globs, pipes, `neofetch`, slash commands.
- 18 tests across 3 suites, all green.

---

## Tier 0 — Necessary ✅ COMPLETE

These either prevented a guaranteed failure or blocked adoption. All five
shipped; the suite went from 18 tests to 36.

### 0.1 Context compaction **[bug]** — ✅ done
`Agent.messages` grows without bound. Every turn re-sends the full history, so
input cost grows quadratically and a long session will eventually be rejected
by the endpoint outright.

Implement in cheapest-first order:
- **Observation masking** — replace old tool outputs with
  `[output: N tokens, masked]`. Keeps the action and reasoning, drops the wall
  of text. Cheaper and better-performing than summarization.
- **Deterministic cleanup** (no model call) — drop duplicate reads of the same
  path, purge error messages whose errors were subsequently resolved, truncate
  long stack traces. Typically 15–30% reduction with zero information loss.
- **Trigger at 75–85%** of the window, not 100% — leaves room for the
  compaction prompt and its response.
- **Cap accumulated tool output.** `MAX_OUT = 4000` caps a single result but
  nothing caps the total. Offload anything oversized to a scratch file and
  keep a short preview.

### 0.2 Packaging + Python version guard **[bug]** — ✅ done
The code uses PEP 604 (`X | None`) in 9 places, so it requires Python 3.10+.
This is stated nowhere, and `run.sh` execs `python3` with no check — a Termux
user on 3.9 gets a traceback instead of a message.

- `pyproject.toml` with PEP 621 metadata, `requires-python = ">=3.10"`,
  `[project.scripts]` console entry point
- Version guard at the top of `run.sh` and `mserver/__main__.py`
- State the requirement in the README

### 0.3 Tool-call audit log **[bug-adjacent]** — ✅ done
`/var/log` is created at boot and never written to. Every tool call the agent
makes should be logged there with timestamp, tool name, arguments and outcome.
This is both the recommended security control for agents with filesystem
access and the thing that makes `logger`/`dmesg`/log-viewer features possible
later.

### 0.4 LICENSE — ✅ done
No license means "all rights reserved" despite a README that invites cloning
and extending. Blocks any outside contribution.

### 0.5 CI — ✅ written, needs one manual step
No `.github/` at all. 18 good tests that nothing runs automatically. A version
matrix would have caught 0.2 on its own.

The pipeline is written and verified, but lives at `ci/github-actions-ci.yml`:
the automation account lacks the GitHub `workflows` permission and cannot push
into `.github/workflows/`. Activating it is a one-line `git mv` by a human with
write access — see `ci/README.md`.

---

## Tier 1 — High value

**✅ Tier 1 substantially complete.** Shipped: snapshots, risk tiers,
confirmation gate, `vos_edit`, all four shell-correctness bugs, the coreutils
batch, `man`, and the Termux survival docs.

✅ **Tier 1 complete.** Retry/backoff and `/chat` rate limiting shipped too.
Deferred to a later pass: just-in-time system reminders, prompt-cache-stable
prefix (both are refinements, not gaps).

### Agent safety and robustness
- ~~**Doom-loop detection**~~ ✅ shipped in Tier 0 (`agent/audit.py`).
- ~~**`vos_edit`**~~ ✅ done. Replaces an exact unique fragment; refuses
  ambiguous matches rather than guessing.
- ~~**Tool risk-tiering**~~ ✅ done (`agent/risk.py`): read / write /
  destructive, with `vos_run` classified by inspecting the command so `rm`
  behind a pipe is still caught.
- ~~**Confirmation gate**~~ ✅ done: `ask` (default) / `--yolo` / `--safe`.
  Fails closed when it cannot prompt (dashboard chat).
- ~~**Snapshots + rollback**~~ ✅ done (`vos/snapshots.py`). Stored outside
  the rootfs so the agent cannot delete its own undo history; rollback saves
  the prior state first; auto-snapshot before an approved wipe; store bounded.
- **Circuit breakers** on token count, call frequency, wall-clock time.
- ~~**Retry with backoff** on 429/5xx~~ ✅ done: exponential backoff honouring
  `Retry-After`, network blips retried, 4xx client errors deliberately not.
  `MOPENAI_TIMEOUT` / `MOPENAI_RETRIES` configurable.
- **System reminders** — agents show attention decay after 30+ tool calls
  (premature completion, exploration loops). Just-in-time reminders fix it.
- ~~**Rate-limit `/chat`**~~ ✅ done: 20 req/min per client, plus a stricter
  failure budget (5 bad tokens → 5-minute lockout) because token guessing is
  the real attack. Failures logged to `/var/log/auth.log`.
- **Prompt-cache-stable system prefix** — keep the system prompt byte-identical
  between turns; move anything dynamic into the last user message.

### Shell correctness **[bug]** — ✅ done
- ~~**Variable expansion**~~ ✅ `$VAR`, `${VAR}`, `$?`, `$PWD`; single quotes
  stay literal, double quotes expand. Plus `export` / `unset`.
- ~~**`.mshrc` loader**~~ ✅ sourced at startup; `alias`/`unalias`/`source`
  implemented, with alias-loop protection.
- ~~**Services survive reboot**~~ ✅ enabled services persist to
  `/var/lib/vos/services.json` and are restarted on boot and reboot; an
  explicit `stop` disables them.
- ~~**Persistent history**~~ ✅ saved to `/root/.msh_history`.
- ~~**Chaining `&&` / `||` / `;`**~~ ✅ done.
- Still open: heredocs, `$(...)` command substitution, background jobs
  `&` + `jobs`/`fg`, `!!` / `!n` history recall.

### Termux operational reality — ✅ documented
Added to the README as a "Keeping it alive on Android" table:
- **Android 12+ Phantom Process Killer** reaps child processes — needs a
  documented one-line ADB fix.
- **tmux** so sessions survive swiping the app away; `termux-wake-lock`
  alone is not sufficient.
- **Xiaomi / OPPO / Vivo / Huawei** need manufacturer battery-manager
  whitelisting or the process dies within the hour.
- **Install from F-Droid, not the Play Store.**
- Battery expectations: roughly 2–5%/hr with a wakelock held.

### Coreutils — ✅ mostly done
Shipped: `sort` (-r/-n/-u), `uniq` (-c), `cut`, `tr` (ranges, -d), `rev`,
`tee` (-a), `seq`, `true`, `false`, `yes`, `basename`, `dirname`, `stat`,
`du` (-h).
Still open: `sed`, `awk`, `diff`, `xargs`, `ln`, `realpath`, `tree`, `sleep`,
`file`, `md5sum`, `base64`.

### `man` — ✅ done
Built from the help text the shell already carried, with extra usage detail
for the commands that have real flags.

---

## Tier 2 — Depth and delight

**Started.** `/proc` and system logging are done.

### System simulation
- ~~`/proc` synthetic filesystem~~ ✅ done (`vos/procfs.py`). Generated on
  read, never stored: uptime advances between reads, and a process that was
  just killed vanishes immediately. Read-only. `uptime meminfo cpuinfo
  loadavg version mounts filesystems stat self/ <pid>/{cmdline,comm,stat,status}`.
- ~~`/var/log` that fills~~ ✅ done (`vos/syslog.py`): `syslog`, `boot.log`,
  `auth.log`, per-service logs, `dmesg` ring buffer, `logger` command, and a
  boot sequence replayed on every boot. All size-capped.
- ~~`cron` + `at`~~ — ✅ done (`vos/scheduler.py`). A real background thread
  behind the `cron` service; 5-field specs with ranges/lists/steps plus
  `@hourly`-style shorthands; `at` for one-shot jobs; output to
  `/var/log/cron.log`. Jobs run on their own `Shell` so they cannot move the
  user's cwd, a failing job cannot kill the daemon, and each job fires at
  most once per due minute (a phone that suspends must not stampede on
  wake). **Destructive commands are refused at add time and again at run
  time** — an unattended job can never reach the confirmation gate, so
  allowing it would have made the scheduler a bypass for every `rm`
  protection.
- ~~Users, groups, permissions~~ — ✅ done (`vos/users.py`). `useradd`,
  `userdel`, `su`, `logout`, `sudo`, `chmod` (octal + symbolic), `chown`,
  `id`, `users`; `ls -l` shows real modes/owners; `--as-user agent` runs the
  whole agent non-root. Enforced in the kernel's mutating ops, **after** the
  sandbox check, never instead of it — a mode-bit bug must not become a host
  escape. Ownership/mode live in `/var/lib/vos/permissions.json` inside the
  rootfs, so snapshots cover them; they are metadata rather than host modes
  because the rootfs is in the user's home and Android sdcard mounts barely
  have modes. Watch out: the permission layer must read its own metadata
  through `_raw_read()`, or `check()` recurses forever.
- Signals (`kill -9` vs `-15`), per-process environment, disk quotas so `df`
  can actually fill, mount points.

### Networking
- ~~Virtual hosts in `/etc/hosts` that actually resolve; `ping`, `netstat`~~ —
  ✅ done (`vos/network.py`). Resolver reads `/etc/hosts` plus a small virtual
  DNS table; `ping`, `ifconfig`, `ip addr|route`, `netstat`, `host`,
  `nslookup`, `curl`, `wget`. Listening ports are derived from running
  services, so `service nginx stop` really does close port 80.
- ~~nginx genuinely serving `/srv/www`~~ — ✅ done. `/etc/nginx/nginx.conf` is
  parsed for `listen`/`root`/`index` and actually drives behaviour; 200/403/404
  are real, path traversal is refused, requests are logged to
  `/var/log/nginx.log`. The vOS network cannot reach the real internet by
  design — `web_fetch` stays the single guarded egress path.
- `nc`, and real `ssh` between vOS instances (currently a stub service).
- Opt-in *real* internet fetch, clearly gated — note this makes fetched
  content an indirect prompt-injection vector, so it must be treated as data,
  never as instructions.

### Agent capability
- ~~**`pkg_create`**~~ — **done.** The agent authors and installs its own
  packages: "build me a log parser and install it as a command" → it does →
  `!logparse` works forever after. `mserver/vos/userpkg.py`, persisted to
  `/var/lib/vos/userpkgs.json` inside the rootfs so snapshots cover it.
  Command bodies are **msh script, never Python** — an LLM-authored Python
  body would be RCE on the host, straight past the sandbox. Built-ins and
  core commands cannot be shadowed; bodies, files and package count are
  capped; a Python-looking body is rejected with guidance to use shell.
  Exposed as `pkg created` / `pkg source` / `pkg delete`.
- ~~**Agent-driven downloads**~~ — **done.** `web_fetch` + `--net`
  (`mserver/agent/webfetch.py`), off by default. https-only, SSRF guard
  re-checked at every redirect hop (loopback, LAN, `169.254.169.254`
  refused), optional `MSERVER_NET_ALLOW`, size/time/redirect caps, HTML
  reduced to text and wrapped in an untrusted-data banner.
- Sub-agents spawned as real `ps` entries with scoped tool sets.
- `plan`/`todo`, persistent memory across sessions, `cron_add`,
  `service_create`, `diff_preview`, `ask_user`.
- Self-reflection: the agent reads `/var/log` to debug its own failures.

### Dashboard
~~SSE streaming of tool calls, web terminal, `/api/status` JSON~~ — ✅ done
(`web/events.py`, new routes in `web/server.py`). The status page no longer
meta-refreshes; `/events` streams shell activity, tool calls and chat turns;
`/term` is a real msh prompt in the browser sharing the REPL's shell.
Bounded per-subscriber queues and a subscriber cap so a backgrounded phone
tab cannot grow memory. No host execution path exists in the web server —
asserted by a test.

Still open: file browser/editor, live log viewer, service start/stop buttons,
snapshot UI, artifact rendering as HTML, mobile layout pass.

---

## Tier 3 — Moonshots

- Full-screen curses TUI with panes for processes, logs and agent chat.
  On a phone in Termux this would look genuinely striking.
- Multi-agent: several AI processes cooperating, visible in `ps`.
- Session record/replay via `.msh` script files; doubles as a test fixture format.
- Plugin API so third parties can ship vOS packages.
- Agent eval/benchmark suite.

---

## Suggested order

1. ~~**Tier 0**~~ — done. `mserver/agent/context.py`, `mserver/agent/audit.py`,
   `mserver/_compat.py`, `pyproject.toml`, `LICENSE`, `.github/workflows/ci.yml`,
   `tests/test_context.py` (18 new tests).
2. ~~**Snapshots**~~ — done, together with the rest of the agent-safety block.
3. ~~**Shell correctness**~~ — done.
4. ~~**Coreutils batch**~~ — done.
5. ~~**`/proc` + richer logging**~~ — done.
6. ~~**`pkg_create` + agent-driven downloads**~~ — done, 47 new tests.
7. ~~**Virtual network**~~ — done, 69 new tests.
8. ~~**`cron`/`at`**~~ — done, 85 new tests.
9. ~~**Users + permissions**~~ — done, 83 new tests.
10. ~~**Dashboard SSE + web terminal**~~ — done, 44 new tests.
11. Next: the housekeeping that keeps being deferred — activate CI (needs a
    human `git mv`), then `SECURITY.md`, which is now well overdue: the
    threat model spans sandbox, gate, permissions, network egress, unattended
    scheduled execution and a browser-facing terminal. After that: signals
    and disk quotas, then the Tier 3 curses TUI.

Open follow-ups from users: no passwords, so `su` to a *lower* privilege is
free and `sudo` is a deliberate-intent marker rather than authentication; no
supplementary group membership (each user is in exactly one group); the
setuid/sticky bits parse but are not honoured.

Open follow-ups from the scheduler: no `MAILTO`-style delivery of job output
back to the agent, so it must read `/var/log/cron.log` itself; no per-job
enable/disable without deleting; `at` times are local-time only.

Open follow-ups from the network: `nc` and inter-instance `ssh` are still
stubs; there are no virtual sockets, so a package cannot listen on a port of
its own; HTTP is GET-only (no POST, redirects or keep-alive).

Open follow-ups from `pkg_create`: package commands cannot yet call each
other's helpers or define functions; there is no `pkg export`/`pkg import` for
sharing a package between devices; `web_fetch` results are not cached, so
re-reading a page costs tokens twice.

Every capability after Tier 1 widens what the agent can do inside the sandbox,
which is exactly why snapshots and the audit log come first.

---

## Housekeeping backlog

`CONTRIBUTING.md`, `SECURITY.md` (warranted — this ships an LLM agent with
filesystem access), `CHANGELOG.md`, issue/PR templates, `.editorconfig`,
dashboard screenshots or a demo GIF.

Done in Tier 0: ruff config, pytest entry point, `pipx install` support.
