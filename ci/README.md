# CI workflow

`github-actions-ci.yml` is the finished CI pipeline for this repo. It lives
here rather than at `.github/workflows/ci.yml` because the automation account
that opened this branch does not hold the GitHub `workflows` permission, so it
is not allowed to push files into `.github/workflows/`.

**To activate it, a human with write access runs one command:**

```sh
mkdir -p .github/workflows
git mv ci/github-actions-ci.yml .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions workflow"
git push
```

(You can delete this README at the same time.)

## What it runs

| Job | What it does |
| --- | --- |
| **test** | The suite on Python 3.10, 3.11, 3.12 and 3.13 — under `pytest`, then each suite standalone the way the README documents for Termux users with no pytest, then a REPL smoke test that installs a package and starts a service. |
| **lint** | `ruff check .` |
| **package** | `python -m build`, installs the wheel, and checks the `mserver` console script runs. |

The version matrix matters specifically: MServer uses PEP 604 union syntax and
so requires Python 3.10+. That constraint was previously undeclared, and a
matrix build is what catches a regression against it automatically.

Everything the workflow runs has been verified locally and passes: 36 tests,
ruff clean, wheel builds and installs.
