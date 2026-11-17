# Architecture

Covers the constraints, numbers and quality targets that apply to the whole system. Only targets that the requirements state are recorded here. Each one lists its proof.

## Constraints

- It's a command-line tool. The command is `bugrepro` and so is the package name.
- Target languages: Python, Java, TypeScript, JavaScript, Go, Rust, C and C++. All eight are in scope; there is no reduced first version.
- Answer keys: Multi-SWE-bench, SWT-bench (Python), TDD-Bench-Java (Java), and closed bug issues filed after the model's training cutoff.
- Code from issues runs only in Docker sandboxes ([decision 0002](../decisions/0002-docker-sandboxes.md)).
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
| Total budget | 20 US dollars in prepaid DeepSeek credit (auto-recharge off) |
| Cost cap per issue | 1 US dollar (default) |
| Time limit per issue | 30 minutes (placeholder until the first backtest) |
| Runs a returned test must fail | 3 out of 3 |

The total budget is held as a prepaid DeepSeek balance owned by G1DO. Auto-recharge is off, so the balance is a hard cap. DeepSeek has no provider-side budget alert; the balance is the control. The model and its cutoff are recorded in [`docs/models.json`](../models.json) and [decision 0005](../decisions/0005-deepseek-direct.md).

## Quality targets

| Attribute | Target | Proof |
| --- | --- | --- |
| Security | Code from issues can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Four planted hostile issues are all blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script. Not built yet. |
| Security | No writes to GitHub. `bugrepro reproduce` works with no token; backtests and collecting issues use a read-only token that stays on the host and never enters a container. | A test run makes zero GitHub write calls. Not built yet. |
| Cost | Spend per issue never goes above the cap (1 US dollar by default). | The run log shows no issue above the cap. Not built yet. |
| Performance | Each issue stops at the time limit (30 minutes, a placeholder). | Covered by the "loops forever" hostile issue above. Not built yet. |
| Reliability | A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. Results on open issues, which have no fix to check against, are labelled "unverified". | Not built yet. |
| Validity | On a backtest issue, the tool sees only the issue as originally filed and the code at the fix's base commit. | The run log shows no access to anything after that commit. Not built yet. |
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
