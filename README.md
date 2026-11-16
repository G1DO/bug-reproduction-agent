# Bug Reproduction Agent

Turns bug reports into verified failing tests.

`bugrepro` is a command-line tool. You give it a bug report from an open-source project. It builds the project's environment in a sandbox and returns either one failing test that reproduces the bug or a "could not reproduce" report that lists each step it tried and what that step printed.

**Status:** the repo is set up, but the tool doesn't do anything yet. `bugrepro` just prints a placeholder.

## Problem

A developer who wants to fix a bug in an open-source project can't start until someone has set up the project and turned the bug report into a failing test by hand. That takes time, and many reports don't have enough detail to do it. (This is an assumption until it's measured.)

Side purpose: public evidence of engineering skill, for hiring.

## Who it's for

- **Developers running the tool:** they want a failing test for a bug report without setting up the project themselves.
- **Readers of the published results:** they want to see how well it works, and to recompute the numbers themselves.

## How success is measured

The main number is the **verified reproduction rate** on the backtest, per language. The **false reproduction** rate is reported next to it.

- **Verified:** the test fails before the fix and passes after it.
- **False:** the test fails before the fix and still fails after it.
- The headline number comes from the set of issues filed after the model's training cutoff. Benchmark results come second.
- Also reported per language: setup success rate, cost per issue, and time per issue.
- Targets: unknown until the first backtest gives a baseline (see [docs/architecture](docs/architecture/README.md)).

## Guarantees

Each guarantee is proven by the check listed for it. None of those checks exist yet, so none of these guarantees hold yet.

| Guarantee | Check | Proven by |
| --- | --- | --- |
| The tool never writes to GitHub and never contacts maintainers. Results go only to the person running it. | A test run makes zero GitHub write calls, and the tool works with a read-only token or with no token. | not yet written |
| Code from an issue can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Four planted hostile issues are all blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script. | not yet written |
| A run stops when an issue's spend reaches the cap. | The log shows no issue above the cap. | not yet written |
| A language counts as supported only once it has an adapter and published results on at least 100 bug issues, failures included. | The published results list at least 100 issues for that language. | not yet written |
| A bug issue is a closed issue with a linked, merged fix that changed at least one test file. | Every backtest issue meets this. | not yet written |
| On a backtest issue, the tool sees only the code from before the fix and the issue as it was filed. | The run log shows no access to anything dated after the issue was filed. | not yet written |
| A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. | Not defined yet. | not yet written |

## How to run

You need [uv](https://docs.astral.sh/uv/) 0.13.7, [Task](https://taskfile.dev) 3.44.1, and Docker.

```sh
task dev       # install the locked environment
uv run bugrepro
task test      # fast tests
task verify    # everything the CI gate runs, except the PR description check
```

`task verify` compares against `origin/main` by default. To compare against a different revision, pass `BASE=<ref>`.

## Where things are

| What | Where |
| --- | --- |
| Code | `src/bugrepro/` (uv's packaged layout) |
| Tests | `tests/` |
| Dependencies, exact versions | `pyproject.toml`, `uv.lock` |
| Python version | `.python-version` |
| Commands (the same ones CI runs) | `Taskfile.yml` |
| CI gate and the scripts it runs | `.github/workflows/gate.yml`, `scripts/` |
| Contract and schema paths watched by the breaking-change check | `.github/contract-paths` (none yet) |
| Constraints, numbers, quality targets | `docs/architecture/README.md` |
| Decision records | `docs/decisions/` |

## Security

See [SECURITY.md](SECURITY.md).
