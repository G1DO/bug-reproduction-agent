# Architecture

Covers the constraints, numbers and quality targets that apply to the whole system. Only targets that the requirements state are recorded here. Each one lists its proof.

## Constraints

- It's a command-line tool. The command is `bugrepro` and so is the package name.
- Target languages: Python, Java, TypeScript, JavaScript, Go, Rust, C and C++. All eight are in scope; there is no reduced first version.
- Answer keys: Multi-SWE-bench, SWT-bench (Python), TDD-Bench-Java (Java), and closed bug issues filed after the model's training cutoff.
- Code from issues runs only in Docker sandboxes ([decision 0002](../decisions/0002-docker-sandboxes.md)).
- The tool never writes to GitHub and never contacts maintainers. Results go only to the person running it.
- On a backtest issue, the tool sees only the code from before the fix and the issue as it was filed.
- A **bug issue** is a closed issue with a linked, merged fix that changed at least one test file.
- A language counts as **supported** only once it has an adapter and published results on at least 100 bug issues, failures included.
- Benchmark issues are run twice: once in an environment the tool builds itself, and once in the benchmark's prebuilt environment. The results show the two separately. Python prebuilt results are scored with SWT-bench's own criterion, and Java prebuilt results with TDD-Bench-Java's.
- For TypeScript, JavaScript, Go, Rust, C and C++, the results show the project's own baseline next to the tool, on the same issues and with the same model.
- A script recomputes the published summary from the published logs, and CI fails if they don't match.
- Deadline: none.

## Numbers

| Number | Value |
| --- | --- |
| Languages | 8 |
| Bug issues per supported language | at least 100 |
| Post-cutoff set | at least 5 repos outside the benchmarks |
| Cost cap per issue | 1 US dollar (default) |
| Time limit per issue | 30 minutes (placeholder until the first backtest) |
| Runs a returned test must fail | 3 out of 3 |

## Quality targets

| Attribute | Target | Proof |
| --- | --- | --- |
| Security | Code from issues can't read credentials, can't reach the network after its dependencies are installed, and is stopped at the time limit. | Four planted hostile issues are all blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script. Not built yet. |
| Security | No writes to GitHub. Works with a read-only token or with no token. | A test run makes zero GitHub write calls. Not built yet. |
| Cost | Spend per issue never goes above the cap (1 US dollar by default). | The run log shows no issue above the cap. Not built yet. |
| Performance | Each issue stops at the time limit (30 minutes, a placeholder). | Covered by the "loops forever" hostile issue above. Not built yet. |
| Reliability | A returned test fails 3 runs out of 3 in the sandbox, and its failure contains the error type or message from the report. If the report has no error message, that part is skipped and the result says so. Results on new issues are labelled "unverified". | Not built yet. |
| Validity | A backtest run sees nothing dated after the issue was filed. | The run log shows no such access. Not built yet. |
| Reproducibility | The published summary matches the published logs. | The recompute script, which runs in CI. Not built yet. |
| Reproduction rate, per language | Unknown | Set once the first backtest gives a baseline. |

## Unknowns

These are not filled in on purpose.

- Total budget: unknown.
- Targets per language: unknown until the first backtest gives a baseline.
- Number of issues in the post-cutoff set: unknown (at least 5 repos).
- Whether the Problem holds: unknown, not yet measured.
- TypeScript, JavaScript, Go, Rust, C and C++ have no published reproduction comparator.
- What the project's own baseline consists of: unknown.
- Whether the Java comparison runs on TDD-Bench-Java's own issues or on Multi-SWE-bench's Java issues: unknown.
- Whether the names `bugrepro` and `bug-reproduction-agent` are free on PyPI, npm and crates.io: not yet checked.

## Diagrams

None yet. The first diagrams (system context, then containers with the sandbox trust boundary marked) get added once there's more than one running part.
