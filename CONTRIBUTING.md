# Contributing to MServer

Thanks for wanting to help. MServer is a small project with a deliberately
narrow set of design constraints — please read them before writing code.

## The four constraints

1. **Pure Python 3 stdlib, no runtime dependencies.** MServer must install on
   Termux from `pkg install python` alone, with no compiler and no wheels.
   Do not add a dependency, not even a "tiny" one. If you need something
   stdlib cannot do, use the vOS to simulate it instead.
2. **Python 3.10 or newer.** PEP 604 union syntax (`X | None`) is used
   throughout. Do not introduce syntax that needs 3.11+.
3. **The agent never reaches the host.** Anything the agent (or a package it
   authors) can do must stay inside the vOS rootfs and the sandbox rules.
   Code that runs an LLM-provided string with `exec`, `eval`, `os.system`,
   `subprocess` against the host, or anything similar, will not be merged —
   that also goes for `pkg_create` package bodies (they must be msh script,
   never Python).
4. **Tests must run without pytest.** Each test suite in `tests/` is a bare
   script (`python3 tests/test_vos.py`) so Termux users with no pytest can
   still run it. Keep new suites self-contained and dependency-free.

## Environment

```sh
git clone https://github.com/trmv2007-bot/MServer.git
cd MServer
python3 -m mserver --help      # quick smoke test
```

Optional dev dependencies (never runtime):

```sh
pip install -e ".[dev]"        # pytest, ruff
```

## Running the tests

All suites are expected to pass **on their own** and with pytest:

```sh
python3 tests/test_vos.py
python3 tests/test_tools.py
python3 tests/test_web.py
python3 tests/test_context.py
python3 tests/test_safety.py
python3 tests/test_shell2.py
python3 tests/test_procfs.py
python3 tests/test_userpkg.py
python3 tests/test_network.py
python3 tests/test_scheduler.py
python3 tests/test_users.py
python3 tests/test_dashboard.py
python3 tests/test_settings.py
```

or

```sh
python3 -m pytest tests/ -v
```

CI runs exactly this — the matrix (3.10–3.13), standalone suites, and a REPL
smoke test — plus `ruff check .` and a wheel build. If it passes locally on
your Python it should pass in CI.

## Lint and style

```sh
ruff check .
```

Configuration lives in `pyproject.toml` (`[tool.ruff]`). Line length is 100;
`E501`/`E741`/`B008` are explicitly ignored with reasons. Match the existing
style: stdlib-only imports, module-level docstrings on new modules, comments
explaining *why*, not what.

## Where things live

| Path | What it is |
| --- | --- |
| `mserver/vos/` | the virtual Linux: kernel, shell, packages, services, /proc, logs, users, network, scheduler |
| `mserver/agent/` | the agent loop, tools, LLM client, risk gate, audit log, context compaction |
| `mserver/web/` | dashboard, SSE events, web terminal |
| `tests/` | dependency-free suites (one file per area, plus a few cross-cutting ones) |

## Adding a vOS package

1. Add a `Package(...)` (files plus optional shell commands) in
   `mserver/vos/packages.py`.
2. Register it in `build_registry()`.
3. Add tests to the relevant suite — at minimum install, command availability,
   and persistence across a restart.

## Adding an agent tool

1. Add an executor + schema in `mserver/agent/tools.py` — the agent picks it
   up automatically.
2. Classify it by risk in `mserver/agent/risk.py` (read / write / destructive).
   If the tool can destroy data, the confirmation gate must cover it.
3. Every tool call already flows through the audit log; keep it that way.

## Safety and security

- Every path the agent touches goes through `VOS.vpath()` — never bypass it
  with a hand-rolled `os.path` check.
- Destructive commands in `vos_run` are detected by inspecting the command
  **including** pipes and `;` chains — keep that detection complete.
- If your change touches authentication, the web server, or anything
  reachable over the network, read [SECURITY.md](SECURITY.md) first and add a
  threat-model note if a control changes.
- Prompt injection is not solved: content fetched from the network is data,
  never instructions. Keep that treatment.

## Pull requests

- Branch from `main`, keep the diff focused — one logical change per PR.
- Run `ruff check .` and the full test suite before pushing.
- Update the tests and, where behaviour is user-visible, the README.
- Add a `CHANGELOG.md` entry under **Unreleased**.
- For changes to the roadmap, note it in `ROADMAP.md` too so the "verified"
  and "open" lists stay honest.

## Reporting a bug

Open an issue with the [bug template](.github/ISSUE_TEMPLATE/bug_report.yml)
— include `mserver --version`, your platform (Termux/Android version or
desktop OS), and the smallest reproduction you can manage.

For security issues, do **not** open a public issue. See
[SECURITY.md](SECURITY.md) for the private reporting path.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE) of this project.
