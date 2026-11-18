# Architecture

Covers the constraints, numbers and quality targets that apply to the whole system. Only targets that the requirements state are recorded here. Each one lists its proof.

## Constraints

- It's a command-line tool. The command is `bugrepro` and so is the package name.
- Target languages: Python, Java, TypeScript, JavaScript, Go, Rust, C and C++. All eight are in scope; there is no reduced first version.
- Answer keys: Multi-SWE-bench, SWT-bench (Python), TDD-Bench-Java (Java), and closed bug issues filed after the model's training cutoff.
- Code from issues runs only in Docker sandboxes ([decision 0002](../decisions/0002-docker-sandboxes.md)). Docker Engine 28.0 or newer is required, and the threat model is in [`docs/threat-model.md`](../threat-model.md). Setup reaches only the hosts on its phase's allowlist through a per-run egress proxy, and the checkout is one commit with the requested tree.
- The tool never writes to GitHub and never contacts maintainers. Results go only to the person running it.
- On a backtest issue, the tool sees only the issue as originally filed and the code at the fix's base commit: the commit the fix's pull request is based on, as the benchmarks record it.
- Backtest dependencies are no newer than the base commit: resolver flags for Python and npm, `go.mod` and `Cargo.lock` for Go and Rust, and date-filtered mirrors for Maven and Gradle and for C and C++ system packages. An ecosystem that turns out too costly to bound is marked `unbounded` for that language only, and the summary shows its share.
- `bugrepro reproduce` works without a GitHub token. Backtests and collecting issues use a read-only token, which stays on the host and never enters a container.
- A **bug issue** is a closed issue with a linked, merged fix that changed at least one test file.
- A language counts as **supported** only once it has an adapter and published results on at least 100 bug issues, failures included.
- Benchmark issues are run twice: once in an environment the tool builds itself, and once in the benchmark's prebuilt environment. The results show the two separately. Python prebuilt results are scored with SWT-bench's own criterion, and Java prebuilt results with TDD-Bench-Java's.
- For TypeScript, JavaScript, Go, Rust, C and C++, the results show the project's own baseline next to the tool, on the same issues and with the same model. The baseline is one shot with the same model: it gets the issue as filed plus the files from the base commit found by searching the issue's words, with no tools, no retries and nothing from the fix.
- The Java comparison uses both TDD-Bench-Java's own issues and Multi-SWE-bench's Java issues, reported separately. An issue in both sets is reported under each and counted once in totals.
- No landing page or other web page. Results are published in the repo.
- A script recomputes the published summary from the published logs, and CI fails if they don't match.
- Deadline: none.

## Numbers

| Number | Value |
| --- | --- |
| Languages | 8 |
| Bug issues per supported language | at least 100 |
| Post-cutoff set | at least 5 repos outside the benchmarks |
| Total budget | 20 US dollars (ceiling; the current prepaid balance is below it) |
| Cost cap per issue | 1 US dollar (default) |
| Time limit per issue | 30 minutes (placeholder until the first backtest) |
| Runs a returned test must fail | 3 out of 3 |

The budget is a 20 US dollar ceiling. The prepaid DeepSeek balance is owned by G1DO, is below the ceiling, and auto-recharge is off, so the balance is a hard cap. DeepSeek has no provider-side budget alert; the balance is the control. The model and its cutoff are recorded in [`docs/models.json`](../models.json) and [decision 0005](../decisions/0005-deepseek-direct.md).

## Quality targets

| Attribute | Target | Proof |
| --- | --- | --- |
| Security | Code from issues can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Proven by the four hostile-issue tests in `tests/test_sandbox.py`: `test_test_phase_has_no_network` and `test_infinite_loop_stops_at_the_limit_and_cleans_up` (#14), `test_issue_code_cannot_read_a_secret` and `test_hostile_install_cannot_read_a_secret_or_reach_the_network` (#16). `test_checkout_is_one_commit_with_base_tree` covers symlink reads and `test_docker_arguments_never_take_host_values` covers Docker arguments. The setup allowlist and phase switch are covered by `test_setup_probes_are_refused` and `test_allow_closes_open_tunnels` (#15). |
| Security | No writes to GitHub. `bugrepro reproduce` works with no token; backtests and collecting issues use a read-only token that stays on the host and never enters a container. | A test run makes zero GitHub write calls. Not built yet. |
| Cost | Spend per issue never goes above the cap (1 US dollar by default). | Proven by #17's tests in `tests/test_model.py` and `tests/test_cli.py`: `test_stops_before_a_call_that_would_exceed_the_cap`, `test_refuses_a_call_whose_worst_case_exceeds_the_cap`, `test_timed_out_attempt_is_charged_its_worst_case`, `test_provider_error_stops_after_three_charged_attempts`, `test_unpriced_model_is_refused_before_any_call`, `test_model_run_logs_each_call_cost_and_the_cap`, `test_cost_cap_and_default_are_in_help` and `test_api_key_appears_in_no_log_line`. The run log's `cost_usd` equals the sum of its calls and never exceeds `cost_cap_usd`. |
| Performance | Each issue stops at the time limit (30 minutes, a placeholder). | The "loops forever" test in `tests/test_sandbox.py` passes (#14). |
| Reliability | A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. Results on open issues, which have no fix to check against, are labelled "unverified". | Not built yet. |
| Validity | On a backtest issue, the tool sees only the issue as originally filed and the code at the fix's base commit. | The checkout is one commit with the base tree and no fix objects (#15). The full #27 check is not built. |
| Reproducibility | The published summary matches the published logs. | The recompute script, which runs in CI. Not built yet. |
| Reproduction rate, per language | Unknown | Set once the first backtest gives a baseline. |

## Unknowns

These are not filled in on purpose.

- Targets per language: unknown until the first backtest gives a baseline.
- Number of issues in the post-cutoff set: unknown (at least 5 repos).
- Whether the Problem holds: unknown, not yet measured.

## Limitations

- Between the filing of an issue and the fix's base commit, maintainers may already have added a related test or partial fix. The benchmarks have the same limit.
- The benchmarks' prebuilt environments aren't limited to dependencies from before the base commit. Their results are marked as not date-bounded and reported apart from self-built results. Files that contain the fix are removed from them before the tool runs.
- SWT-bench's Django issues are tracked on Django's own tracker, not in GitHub issues, so they aren't bug issues here. The SWT-bench comparison covers only the SWT-bench issues that pass the bug-issue rule, and says so beside SWT-bench's full-set numbers.
- TypeScript, JavaScript, Go, Rust, C and C++ have no published reproduction comparator. The project's own one-shot baseline is their comparator.

## Package names

On 2026-11-16, `bugrepro` and `bug-reproduction-agent` were both unregistered on PyPI, npm and crates.io. Nothing has been reserved, so this can change before the first release.

## Diagrams

None yet. The first diagrams (system context, then containers with the sandbox trust boundary marked) get added once there's more than one running part.
