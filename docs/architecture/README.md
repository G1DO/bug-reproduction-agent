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
- Python setup uses `uv pip install --exclude-newer <base commit time> --reinstall` on a digest-pinned full `python:<minor>` image (3.8, 3.9 or 3.10, chosen only from versions released by the base time). It runs as UID 1000 with no system packages. After setup, it lists every installed version and looks up each version's first upload time on PyPI, recording any version uploaded after the base time. The PEP 660 fallback uses tool-owned current setuptools and wheel outside the audited venv. Matplotlib's qhull build tool is `cmeel-qhull==8.0.2.1` from PyPI, also outside the audited venv; it packages qhull 8.0.2, which predates the base times where it is used.
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
| Python setup images | `python:3.8`, `python:3.9`, `python:3.10`, each pinned by digest |
| Python setup tooling | `uv==0.13.7`; `cmeel-qhull==8.0.2.1` for matplotlib builds |

The budget is a 20 US dollar ceiling. The prepaid DeepSeek balance is owned by G1DO, is below the ceiling, and auto-recharge is off, so the balance is a hard cap. DeepSeek has no provider-side budget alert; the balance is the control. The model and its cutoff are recorded in [`docs/models.json`](../models.json) and [decision 0005](../decisions/0005-deepseek-direct.md).

## Quality targets

| Attribute | Target | Proof |
| --- | --- | --- |
| Security | Code from issues can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Proven by the four hostile-issue tests in `tests/test_sandbox.py`: `test_test_phase_has_no_network` and `test_infinite_loop_stops_at_the_limit_and_cleans_up` (#14), `test_issue_code_cannot_read_a_secret` and `test_hostile_install_cannot_read_a_secret_or_reach_the_network` (#16). `test_checkout_is_one_commit_with_base_tree` covers symlink reads and `test_docker_arguments_never_take_host_values` covers Docker arguments. The setup allowlist and phase switch are covered by `test_setup_probes_are_refused` and `test_allow_closes_open_tunnels` (#15). #17's host-side model key and provider replies are covered by `test_api_key_appears_in_no_log_line`, `test_transport_error_containing_the_key_is_redacted` and `test_api_key_with_a_control_character_is_refused`, plus `test_oversized_provider_response_is_refused`, `test_truncated_empty_reply_is_an_error`, `test_case_variant_length_still_checks_for_an_answer`, `test_redacted_only_truncated_reply_is_an_error` and `test_configured_model_fallback_is_redacted_and_bounded`. #20's file tools are covered by `test_file_tools_are_isolated_from_project_modules`, the list/read/root-symlink tests, the write refusals, the surrogate tests and `test_run_test_runs_once_in_a_new_no_network_container` in `tests/test_reproduce.py`. |
| Security | No writes to GitHub. `bugrepro reproduce` works with no token; backtests and collecting issues use a read-only token that stays on the host and never enters a container. | A test run makes zero GitHub write calls. Not built yet. |
| Cost | Spend per issue never goes above the cap (1 US dollar by default). | Proven by #17's tests in `tests/test_model.py` and `tests/test_cli.py`: `test_stops_before_a_call_that_would_exceed_the_cap`, `test_refuses_a_call_whose_worst_case_exceeds_the_cap`, `test_timed_out_attempt_is_charged_its_worst_case`, `test_timeout_stops_after_three_charged_attempts`, `test_provider_error_stops_after_three_charged_attempts`, `test_time_limit_abort_still_logs_spent_cost`, `test_unpriced_model_is_refused_before_any_call`, `test_cache_hit_usage_is_charged_at_the_cache_hit_price`, `test_model_run_logs_each_call_cost_and_the_cap`, `test_low_cost_cap_stops_before_any_call`, `test_cost_cap_and_default_are_in_help` and `test_api_key_appears_in_no_log_line`. #19's setup model fallback uses the same budget: `test_setup_stops_at_the_cost_cap` and `test_setup_refuses_a_model_with_a_different_budget` in `tests/test_python_adapter.py`. #20's loop maps the client's stop with `test_cost_cap_stops_the_loop` and refuses a mismatched budget with `test_reproduce_refuses_a_model_with_a_different_budget` in `tests/test_reproduce.py`. The run log's `cost_usd` equals the sum of its calls and never exceeds `cost_cap_usd`. |
| Performance | Each issue stops at the time limit (30 minutes, a placeholder). | The "loops forever" test in `tests/test_sandbox.py` passes (#14). #17's model calls use the time left before the deadline: `test_expired_deadline_makes_no_call` and `test_retry_sleep_does_not_pass_the_deadline` in `tests/test_model.py`. #19's Python setup uses the same deadline: `test_setup_stops_at_the_time_limit` in `tests/test_python_adapter.py`. #20's loop is covered by `test_time_limit_stops_the_loop`, `test_tool_timeout_is_logged_and_stops` and `test_run_test_timeout_is_logged` in `tests/test_reproduce.py`. |
| Reliability | A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. Results on open issues, which have no fix to check against, are labelled "unverified". | The 3/3 and error-match parts are proven by #1's tests in `tests/test_verify.py`: `test_three_failing_runs_with_the_report_error_are_returned`, `test_two_of_three_failures_is_not_returned`, `test_failure_without_the_report_error_is_not_returned`, `test_error_match_ignores_the_test_path`, `test_error_match_ignores_printed_error`, `test_error_match_ignores_the_test_source`, `test_error_match_ignores_a_final_line_in_an_assertion_diff`, `test_output_after_the_pytest_report_is_ignored`, `test_short_form_frames_in_captured_output_are_ignored`, `test_test_that_raises_the_report_message_itself_is_not_returned`, `test_failure_raised_in_the_test_file_is_not_returned`, `test_report_without_an_error_message_skips_the_match`, `test_collection_error_is_not_a_reproduction`, `test_docker_failure_is_an_error_and_the_test_is_not_returned`, `test_could_not_reproduce_report_lists_every_step_with_its_command_and_output` and `test_scripted_model_test_is_verified_in_three_new_no_network_containers`; plus `test_error_patterns_reads_a_full_pytest_log`, `test_failure_raising_file_reads_pytest_long_form`, `test_failure_error_text_excludes_paths_source_and_captured_output` and `test_failure_raising_file_ignores_frames_in_captured_output` in `tests/test_python_adapter.py`. `test_result_line_carries_a_label_only_when_set` covers the label field; #23 sets `unverified` on open issues. |
| Validity | On a backtest issue, the tool sees only the issue as originally filed and the code at the fix's base commit. | The checkout is one commit with the base tree and no fix objects (#15). Python setup uses the exact base-commit time and `--reinstall`, and its installed-version audit writes a later upload into the result line. Proven by `tests/test_python_adapter.py`: `test_install_uses_exact_utc_bound_and_reinstall`, `test_setup_refuses_python_released_after_the_base_time`, `test_git_dependency_is_setup_failed`, `test_dynamic_git_dependency_is_setup_failed`, `test_url_dependency_on_an_allowlisted_host_is_unbounded`, `test_audit_flags_a_version_uploaded_after_the_base_time`, `test_audit_sends_no_token`, `test_result_line_records_newer_versions` and `test_spike18_issues_setup_as_predicted`. The full #27 check is not built. |
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
