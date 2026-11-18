# Bug Reproduction Agent

Turns bug reports into verified failing tests.

`bugrepro` is a command-line tool. You give it a bug report from an open-source project. It builds the project's environment in a sandbox and returns either one failing test that reproduces the bug or a "could not reproduce" report that lists each step it tried and what that step printed.

**Status:** the sandbox can fetch one commit in the trusted fetcher container, stream it into a setup container whose only route out is the per-run egress proxy, freeze it, and run tests with no network. The reproduce workflow is not built yet.

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

Each guarantee is proven by the check listed for it. Some of those checks exist now; the table says which ones.

| Guarantee | Check | Proven by |
| --- | --- | --- |
| The tool never writes to GitHub and never contacts maintainers. Results go only to the person running it. | A test run makes zero GitHub write calls. `bugrepro reproduce` works with no token; backtests and collecting issues use a read-only token, which stays on the host and never enters a container. | not yet written |
| Code from an issue can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Four planted hostile issues are all blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script. | Proven by the four hostile-issue tests: `test_test_phase_has_no_network` and `test_infinite_loop_stops_at_the_limit_and_cleans_up` (#14), `test_issue_code_cannot_read_a_secret` and `test_hostile_install_cannot_read_a_secret_or_reach_the_network` (#16). The symlink reads in `test_checkout_is_one_commit_with_base_tree` and `test_docker_arguments_never_take_host_values` cover the file-read and argument paths. The setup allowlist and phase switch remain proven by `test_setup_probes_are_refused` and `test_allow_closes_open_tunnels` (#15). |
| A run stops when an issue's spend reaches the cap. | The log shows no issue above the cap. | Proven by #17's tests in `tests/test_model.py`: `test_stops_before_a_call_that_would_exceed_the_cap`, `test_refuses_a_call_whose_worst_case_exceeds_the_cap`, `test_timed_out_attempt_is_charged_its_worst_case`, `test_timeout_stops_after_three_charged_attempts`, `test_unpriced_model_is_refused_before_any_call` and `test_cache_hit_usage_is_charged_at_the_cache_hit_price`; and in `tests/test_cli.py`: `test_provider_error_stops_after_three_charged_attempts`, `test_model_run_logs_each_call_cost_and_the_cap`, `test_low_cost_cap_stops_before_any_call`, `test_cost_cap_and_default_are_in_help` and `test_api_key_appears_in_no_log_line`. The log's total equals the sum of its calls and never exceeds `cost_cap_usd`. |
| A language counts as supported only once it has an adapter and published results on at least 100 bug issues, failures included. | The published results list at least 100 issues for that language. | not yet written |
| A bug issue is a closed issue with a linked, merged fix that changed at least one test file. | Every backtest issue meets this. | not yet written |
| On a backtest issue, the tool sees only the issue as originally filed and the code at the fix's base commit. | The run log shows no access to anything after that commit. | Partly proven: #15's checkout is one commit with the base tree and no fix objects. The full validity check is #27. |
| A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. Results on open issues, which have no fix to check against, are labelled "unverified". Otherwise the tool returns a "could not reproduce" report that lists each step tried and its output. | Each returned test is run 3 times in the sandbox and its failure is matched against the report before it's returned. | not yet written |

## Limitations

- Between the filing of an issue and the fix's base commit, maintainers may already have added a related test or partial fix. The benchmarks have the same limit.
- The benchmarks' prebuilt environments aren't limited to dependencies from before the base commit. Their results are marked as not date-bounded and reported apart from results in environments the tool builds itself.
- SWT-bench's Django issues are tracked on Django's own tracker, not in GitHub issues, so they don't count as bug issues here. The SWT-bench comparison covers only the SWT-bench issues that do.
- TypeScript, JavaScript, Go, Rust, C and C++ have no published reproduction numbers to compare with. For those languages, the project's own one-shot baseline is the comparator.

## How to run

You need [uv](https://docs.astral.sh/uv/) 0.13.7, [Task](https://taskfile.dev) 3.44.1, Docker Engine 28.0 or newer, `git` for the checks and test fixtures, and `openssl` for the fixtures.

```sh
task dev       # install the locked environment
uv run bugrepro
task test      # tests, including the Docker sandbox tests
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
| Model IDs, cutoffs and source URLs | `docs/models.json` |
| Config names, never values | `.env.example` |
| Constraints, numbers, quality targets | `docs/architecture/README.md` |
| Threats and what is not covered yet | `docs/threat-model.md` |
| Decision records | `docs/decisions/` |

## Security

See [SECURITY.md](SECURITY.md).
