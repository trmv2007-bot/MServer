# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `CONTRIBUTING.md`, `.editorconfig`, issue/PR templates.
- CI pipeline finalised and documented at `ci/github-actions-ci.yml`
  (activation still needs the GitHub `workflows` permission).

## [1.0.0] - 2026-09-03

### Added
- **MServerOS** (`mserver/vos`): sandboxed rootfs with the `msh` shell
  (pipes, redirection, globs, `&&`/`||`, variables, aliases, `.mshrc`,
  persistent history), `pkg` manager, `service` manager, process table,
  users/groups/mode bits, synthetic `/proc`, filling `/var/log` + `dmesg`,
  `cron`/`at`, virtual network (DNS, ports, nginx), snapshots, and
  `pkg_create` for agent-authored packages.
- **Agent** (`mserver/agent`): LLM loop with tools (`vos_run`, `vos_read/
  write/edit`, `pkg_install`, `web_fetch`, `present`, `dashboard`, …),
  offline planner, any OpenAI-compatible endpoint, retry/backoff, context
  compaction, doom-loop detection, tool-call audit log, and a deterministic
  risk gate + confirmation prompt.
- **Dashboard** (`mserver/web`): zero-dependency web UI with system info,
  processes, storage, packages, artifacts, agent chat, SSE tool-call stream,
  and a browser web terminal.
- **Packaging**: `pyproject.toml` (PEP 621, PEP 604 version guard,
  `mserver` console script), `LICENSE`, `SECURITY.md`, `ROADMAP.md`,
  Python 3.10+ check in `run.sh`.

### Tests
- 498 tests across 13 dependency-free suites (each also runs as a bare
  script), plus a REPL smoke test in CI.
