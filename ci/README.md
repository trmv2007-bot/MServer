# CI workflow

`github-actions-ci.yml` is the finished CI pipeline for this repo. It lives
here rather than at `.github/workflows/ci.yml` because the automation account
that opens Arena branches does not hold the GitHub `workflows` permission, so
it is not allowed to create or update files under `.github/workflows/` — a
push or a contents-API write is refused with:

```
! [remote rejected] ... (refusing to allow a GitHub App to create or update
workflow `.github/workflows/ci.yml` without `workflows` permission)
```

**To activate it, a human with an owner token (or a GitHub App/PAT with the
`workflows: write` permission) runs one command in their own checkout:**

```sh
mkdir -p .github/workflows
git mv ci/github-actions-ci.yml .github/workflows/ci.yml
git rm ci/README.md
git commit -m "ci: activate GitHub Actions workflow"
git push
```

(This repository already contains the `CONTRIBUTING.md`, `CHANGELOG.md`,
issue/PR templates and `.editorconfig`; no other setup is needed.)

If you're using Arena to do this, reconnect the GitHub integration with a
token that has the `workflows` permission and the agent can complete the
move and push for you.

## What it runs

| Job | What it does |
| --- | --- |
| **test** | The suite on Python 3.10, 3.11, 3.12 and 3.13 — under `pytest` (498 tests, 13 suites), then each suite standalone the way the README documents for Termux users with no pytest, then a REPL smoke test that installs a package and starts a service. |
| **lint** | `ruff check .` |
| **package** | `python -m build`, installs the wheel, and checks the `mserver` console script runs. |

The version matrix matters specifically: MServer uses PEP 604 union syntax and
so requires Python 3.10+. That constraint was previously undeclared, and a
matrix build is what catches a regression against it automatically.

Everything the workflow runs has been verified locally and passes: 498 tests,
ruff clean, wheel builds and installs.
