# Threat model

Covers the sandbox from issues #14, #15 and #16, the host-side model call from
#17, the Python setup from #19, the test-writing loop from #20, and the
verification from #1: the code that fetches a project, runs its setup, installs
its dependencies, lets a model read the project and write one test, runs that
test three times to check it reproduces the report, and executes a test written
from issue text. Update this document when a change adds a new way for that
code to reach something it shouldn't.

## What are we building?

`bugrepro` runs code from a bug report's project and tests written from the
report. None of it is trusted. One issue run gets its own networks, containers
and images, and the rest of the machine stays out of reach.

The trust boundary has three phases:

1. **Fetch.** A trusted fetcher container, from a digest-pinned image with git,
   joins the run's internal network. The egress proxy allows `github.com`
   only, for the fetcher. The fetcher downloads the requested commit, rewrites
   it to one root commit with the same tree, and verifies the object set. The
   checkout is streamed directly into setup in the next phase; the host keeps
   no copy.
2. **Install.** The setup container joins only the run's internal network. Its
   only route out is the egress proxy, which allows the adapter's registries
   for the install phase. The verified checkout is streamed into it from the
   fetcher as a tar stream; the host keeps no copy. Python setup reads project
   metadata in the trusted fetcher to choose a digest-pinned image, then runs
   as UID 1000 with no system packages. If deterministic collection fails, the
   model may propose setup commands; the harness validates them and runs them
   in the same container.
3. **Test.** The setup container is frozen to an image and removed. Every test
   run starts a new container from that image with `--network none`.
   Verification repeats that run three times for the chosen test and checks
   each failure against the report.

The project image is never trusted with `github.com`. Only the fetcher, which
runs a pinned image and no project code, sees the fetch allowlist.

The model client from #17 is not project code. It runs on the host, sends one
request to the configured provider, and holds the model key for the length of
the call.

The test-writing loop from #20 is a host-side driver. It gives the model only
the issue title and body from the record. Its four tools run in the setup
container: `list_files`, `read_file`, `write_test` and `run_test`. It writes at
most one test file in an existing test directory, logs every call and output,
and hands the test to verification. `run_test` freezes the setup image and runs
the file in a new container with no network.

Assets worth protecting:

- the model API key and any GitHub token;
- files outside the container, including the user's home directory and SSH keys;
- the Docker daemon and its socket;
- the host network and other machines on it;
- other issue runs;
- the published results, which must reflect the code at the issue's base commit.

## What can go wrong?

- A test reaches the network after setup: it can exfiltrate data, download the
  fix, or call the host.
- Setup reaches more than package registries: the cloud metadata service, the
  host, the local network, or GitHub, where it could read the fix.
- Project metadata selects a Python newer than the base commit, or points
  setup at a git or URL dependency outside the checkout.
- A model-proposed setup command reaches a git host, changes the date bound,
  changes the package index, or writes outside the checkout.
- Instructions planted in an issue, or in project files the model reads, steer
  the model into a different task.
- A model-written test escapes the checkout, overwrites an existing test, or
  writes outside the project's existing test directories.
- A project module named like a standard library module shadows the file-tool
  script, or the checkout root is replaced with a symlink.
- A test path is a pytest node id or an option, so `run_test` executes a
  different test.
- A model-written test reaches the network or host files during the loop.
- A returned test fails for a different reason than the report, or raises the
  report's message itself instead of reproducing the bug.
- The model keeps reading or writing until the issue's spend or time limit is
  passed.
- An installed package version was published after the base commit.
- The registry audit's JSON is oversized or malformed, or a package has no
  upload time.
- The qhull build tool from PyPI is newer than the base commit even though the
  qhull source it packages predates the base time.
- A prebuilt project image's entrypoint or fake `git` uses the fetch allowlist
  to clone the fix before the tool's own fetch.
- A hostname resolves to a private, link-local or multicast address, or changes
  between the check and the connection.
- A tunnel opened under one phase survives into the next.
- Code reads credentials or host files.
- The model API key is logged, written into a result line, or passed into a
  sandbox container.
- A redirect or a provider diagnostic copies the key to a host the user did
  not configure.
- A provider reply is oversized, malformed or truncated, exhausting memory or
  being logged as a usable answer.
- Code runs forever, forks without limit, or fills the disk.
- Code prints without limit, exhausting host memory, or floods the proxy log.
- Setup plants files that change what the tests do.
- A process started during setup survives into the test phase.
- A failed or interrupted run leaves containers, images or networks behind.
- A container escapes the shared kernel.

## What will we do about it?

- **Trusted fetcher.** The fetch runs in a separate container from a pinned
  image with git. The project image never gets the `github.com` allowlist.
  The host never clones.
- **Predictable start.** The setup and fetcher containers start with the tool's
  `sleep` as ENTRYPOINT, so the image's entrypoint does not run before the
  tool's commands. Test containers run the selected command as ENTRYPOINT.
- **One isolated network, one proxy.** The setup network is `--internal` with
  `gateway_mode_ipv4=isolated` and `gateway_mode_ipv6=isolated`, so the bridge
  has no address on the host. The proxy is the only container also attached to
  a dedicated egress bridge.
- **Proxy checks.** The proxy allows exact hostnames only, refuses IP literals,
  resolves each name once, refuses if **any** address is not public unicast,
  connects to the checked address rather than re-resolving, and allows port 443
  in production. `is_public_unicast` rejects multicast, unspecified, reserved,
  link-local and IPv6 site-local forms. It unwraps IPv4-mapped forms and
  applies the IPv4 checks; `resolve_once` strips IPv6 zone IDs. `is_global`
  alone is not enough because it reports multicast addresses as global.
- **Phase-bound allowlists.** Fetch allows `github.com` only. Install allows the
  adapter's registries and rejects `github.com`. Test has no network. A caller
  cannot use the generic allowlist to widen a phase.
- **Pinned Python and exact time.** Python setup only chooses a digest-pinned
  `python:<minor>` image for a version released by the base time. It passes the
  exact UTC RFC 3339 time to `uv pip install --exclude-newer ... --reinstall`.
  Git dependencies fail with `git-dependency` because their hosts are off the
  install allowlist. HTTP(S) URL dependencies on allowlisted hosts are recorded
  as `unbounded`. `UV_PYTHON_DOWNLOADS=never` stops uv from fetching another
  interpreter from `releases.astral.sh`.
- **Installed-version audit.** After setup the venv lists every installed
  distribution. The host looks up each version's first upload time on PyPI with
  no token, no redirects, a response cap, strict package-name and version
  validation, and three bounded attempts. A version first uploaded after the
  base time is written into the result line; a missing or malformed upload time
  fails setup with `audit-failed`.
- **Model setup commands.** The model is called only when deterministic
  collection fails. It gets the exact time and failure output, not host paths
  or secrets. Commands are JSON lists, limited to `uv`, `python`, `python3` and
  `pytest`; the validator rejects git hosts, non-allowlisted URLs, alternate
  indexes, parent paths, shell characters, arbitrary `python -c`, and any date
  bound other than the exact one. The harness injects `--exclude-newer` and
  `--reinstall`. At most
  two turns and five commands per turn run; the budget and deadline still stop
  the run.
- **Issue text is data.** The reproduce loop gives the model only the issue
  title and body from the record. It has no GitHub tool and no secrets. The
  loop's tools are the only way it can read the project or write a file.
- **Sandbox-only file tools.** `list_files` and `read_file` run a fixed script
  inside the setup container with `python -I` and a cwd outside the checkout,
  so project modules cannot shadow its imports. The script requires the
  checkout root to be a real directory, not a symlink, then resolves each path
  under it and refuses absolute paths, parent components, paths outside the
  root and symlinks that escape it. `write_test` requires a `test_*.py` or
  `*_test.py` filename; refuses `::`, `[` and `]`, a first component starting
  with `-` or `@`, absolute paths, `..`, paths outside a test directory that
  already contains a test file, existing files, symlinked parent directories
  and a second test file. It writes one file with an exclusive create, and no
  host file is staged. A truncated listing or command output is marked for the
  model.
- **One test, one fresh no-network container.** `run_test` freezes the setup
  image, runs the written file once with `run_isolated` in a new container with
  `--network none`, and restarts the file tools from the same image. The test
  content is untrusted, but it has no proxy environment, capabilities or host
  mounts. A timeout or sandbox failure before a command result is written to
  the steps log with the error.
- **Cap and deadline.** Every model turn uses the #17 client and the run's
  budget. Every tool call uses the run's deadline. Every tool call and its
  output goes into the steps log.
- **Verification.** The loop hands its one test to #1. Verification will reject
  a test that does not fail for the report's error, and a collection error will
  not count as a reproduction.
- **Matplotlib build tool.** Matplotlib's build normally downloads FreeType and
  qhull source tarballs. Python setup uses the image's system FreeType and a
  pinned `cmeel-qhull==8.0.2.1` wheel from PyPI, installed outside the audited
  venv and linked with an rpath. The wheel packages qhull 8.0.2, which predates
  the matplotlib base time used here. No install host beyond PyPI is added.
- **Private control channel.** The host changes the allowlist over a Unix
  socket inside the proxy container, using `docker exec`. No control listener
  is on the internal network, so setup cannot widen its own allowlist. The
  proxy acknowledges after closing tunnels from the previous generation, and a
  generation check stops a request that started under the old list from opening
  a new tunnel.
- **Single-commit checkout.** The fetcher fetches one SHA with `--depth 1`,
  builds a new root commit with `git commit-tree <sha>^{tree}`, removes the
  shallow file, `FETCH_HEAD`, fetched tags, extra refs and reflogs, and prunes.
  The proof is object-level: exactly one commit object, no shallow or alternate
  objects, one ref, no unreachable objects, and a tree equal to the requested
  commit's tree. The project image cannot start while the proxy is in Fetch,
  and `fetch_checkout` switches to Install with an empty allowlist before it
  yields.
- **Stream in, never mount.** The verified checkout is streamed directly from
  the trusted fetcher into setup as a tar stream. The host relays bytes and
  never writes a checkout to disk. Host paths are never mounted, and nothing is
  copied out of a container that ran project code.
- **No host environment or credentials.** No host environment values, no
  bind mounts, and never `/var/run/docker.sock`. The only environment the tool
  sets is the fixed proxy environment and fixed tool values. Every `docker`
  call runs with `DOCKER_CONFIG` set to an empty directory the tool owns, so
  the host's client config, including proxy settings and credential helpers,
  never applies.
- **Model key stays on the host.** `src/bugrepro/model.py` reads
  `BUGREPRO_API_KEY` from the host environment, sends it only in the
  `Authorization` header of the request to `BUGREPRO_BASE_URL`, and refuses
  redirects. The run log records the model ID, token counts, each call's cost
  and the cap, never the request headers or the key. Provider errors are
  redacted before they reach the log. No model call runs in a container.
  Provider replies are untrusted too: token counts are checked against the
  quoted bound, the body is capped at 4 MiB, and a reply that stops at the
  output limit without text is an error.
- **Per-exec proxy environment.** Proxy variables are set on each setup
  `docker exec`, not on the container or the frozen image. Test containers
  clear them.
- **Least privilege.** Every container runs as UID 1000, with `--cap-drop ALL`
  and `--security-opt no-new-privileges`.
- **One run, one label.** Every container and network the run creates, and
  every image it commits, carries `bugrepro.run=<run id>`. Cleanup attempts to
  remove all of them by label and reports anything it cannot remove.
- **One deadline.** The run gets 30 minutes by default, set with
  `--time-limit`. Commands that run project code use the time that is left. At
  the deadline the run is stopped, labelled objects are removed, and the
  outcome is `stopped-at-time-limit`. Cleanup and kill calls use short fixed
  timeouts and may return after the deadline.
- **Verification.** The chosen test runs three times from the frozen image with
  no network. The command pins verbose, long, uncoloured, captured output with
  live logging off and a failed/error summary; a project's `addopts` cannot
  remove those. Each run must report exactly one failed test and no setup or
  teardown errors in pytest's real footer, and the footer counts must match
  pytest's progress line; a footer-shaped line printed in captured output cannot
  stand in for the real one. The match takes its exception text
  and frame only from the first real failure block before the footer. It does
  not use the test path, parametrize ids, source lines, captured output, summary
  messages or anything printed after the footer as a frame. Assertion diff text
  can match a report pattern, but the frame check refuses it when its frame is
  the test file. Each exception is paired with its own final traceback line, so
  a chained exception cannot borrow another exception's frame. A matched
  message must not appear in the test file, and the raising frame must be a
  `.py` file outside the test file; `<string>` and `<stdin>` frames are refused.
  With no error in the report the match is skipped and recorded.
- **Bounded output.** The host keeps at most 1 MiB of stdout and 1 MiB of
  stderr per command and marks the result when it truncates. The proxy caps its
  own log lines.
- **Version gate.** Docker Engine 28.0 or newer is required before any sandbox
  starts, and before any model call. 28.0 is needed for the isolated gateway
  and its DNS behavior on internal networks.
- **Proof.** `tests/test_sandbox.py` plants the hostile probes: reading a
  secret, calling an outside host, looping forever, a hostile install script,
  the symlink fixture, the phase-switch tunnel, the entrypoint and image-ref
  checks, the streamed checkout proof, the Docker-argument check and the
  test-only override checks. `tests/test_egress_proxy.py` covers the parser,
  the address classifier, DNS rebinding, generation closure and the control
  protocol. `tests/test_python_adapter.py` covers the Python image choice, the
  exact date bound, git and URL dependencies, the installed-version audit, the
  model command validator, non-root setup and the five-issue spike run.
  `tests/test_reproduce.py` covers the #20 loop: the scripted-model steps log,
  the done and give-up ends, the budget and time stops, the tool-failure path,
  and the Docker tests for the path refusals and the single no-network test
  run. `tests/test_verify.py` covers #1: the 3/3 rule, the two-of-three and
  collection refusals, the report-error match, the path/print/source bypasses,
  the message-in-test and raising-frame refusals, the skipped match, the Docker
  error, the could-not-reproduce step list, the optional label and a
  scripted-model run through three fresh no-network containers with a real
  pytest.

## Did we do enough?

For #15's and #16's threats, yes: the hostile probes all fail, the proxy logs
each proxy-mediated refusal with host and time until its line cap, open tunnels
close at the phase switch, the project image cannot start in Fetch, and the
checkout is one verified commit with no fix object.

For #16, `test_issue_code_cannot_read_a_secret` plants the canary in the host
environment, a home-directory file and the Docker client's proxy config; no
command output, proxy log or container environment contains it.
`test_hostile_install_cannot_read_a_secret_or_reach_the_network` checks setup's
reads and writes, and the planted `sitecustomize.py` and `.pth` files in the
test phase cannot read the canary or connect out.
`test_checkout_is_one_commit_with_base_tree` checks the symlink fixture and
gets no host content back. `test_docker_arguments_never_take_host_values`
checks that the Docker arguments carry no host environment or host path.

For #17, `test_api_key_appears_in_no_log_line` runs the model command with a
canary key and a provider that echoes the key in a 401, in a successful reply
and in response metadata. The canary appears in neither stdout nor stderr.
`test_transport_error_containing_the_key_is_redacted` and
`test_api_key_with_a_control_character_is_refused` cover the other key paths.
`test_oversized_provider_response_is_refused`,
`test_truncated_empty_reply_is_an_error`,
`test_case_variant_length_still_checks_for_an_answer`,
`test_redacted_only_truncated_reply_is_an_error` and
`test_truncated_reply_with_text_is_reported` cover untrusted replies.
`test_configured_model_fallback_is_redacted_and_bounded` covers the log's
model fallback. The spend tests in `tests/test_model.py` show the cap holds
before a call, after a call and after a timed-out attempt.

For #19, `test_python_setup_collects_a_fixture_projects_tests` runs the full
non-root setup against a real PyPI, and `test_spike18_issues_setup_as_predicted`
runs the five #18 issues. `test_install_uses_exact_utc_bound_and_reinstall` checks
the resolver flags, `test_git_dependency_is_setup_failed` and
`test_dynamic_git_dependency_is_setup_failed` the git outcome,
`test_url_dependency_on_an_allowlisted_host_is_unbounded` the unbounded record,
and `test_audit_flags_a_version_uploaded_after_the_base_time` the post-setup
audit. `test_audit_sends_no_token` checks the audit sends no credential.
`test_every_setup_step_logs_command_output_exit_and_duration` checks the step
log, and `test_model_command_cannot_change_the_date_bound`,
`test_model_python_c_command_is_refused` and
`test_setup_refuses_an_extra_dependency_option` check untrusted input.
`test_setup_refuses_python_released_after_the_base_time` checks the Python
version bound, and `test_setup_refuses_a_model_with_a_different_budget` checks
the budget wiring. `test_matplotlib_build_uses_the_pinned_qhull_tool` checks the
build-tool path. The five-issue test is opt-in with `BUGREPRO_RUN_SPIKE=1`; the
gate runs the fixture and unit tests.

For #20, `test_file_tools_read_the_fixture_checkout` reads a real checkout,
and `test_file_tools_are_isolated_from_project_modules` proves a project root
`json.py`/`pathlib.py` cannot shadow the tool script.
`test_list_files_refuses_a_path_outside_the_checkout`,
`test_read_file_refuses_a_path_outside_the_checkout` and
`test_read_file_refuses_a_symlinked_checkout_root` cover the root jail.
`test_write_test_refuses_an_absolute_path`,
`test_write_test_refuses_a_parent_path`,
`test_write_test_refuses_a_path_outside_existing_test_directories`,
`test_write_test_refuses_an_existing_file`,
`test_write_test_refuses_a_symlinked_directory`,
`test_write_test_refuses_a_second_test_file`,
`test_write_test_refuses_a_non_test_filename`,
`test_write_test_refuses_a_pytest_node_id` and
`test_write_test_refuses_a_pytest_option_path` cover each write refusal, while
`test_write_test_accepts_a_test_py_directory` and
`test_write_test_accepts_a_nested_test_directory` cover the allowed test
directories.
`test_run_test_runs_once_in_a_new_no_network_container` checks the hostname,
the exact test command, one execution and that a test-container side effect is
absent from the restarted image. `test_every_tool_call_and_output_is_logged`,
`test_tool_timeout_is_logged_and_stops`, `test_run_test_timeout_is_logged` and
`test_truncated_tool_output_is_marked_for_the_model` check the steps log.
`test_cost_cap_stops_the_loop`, `test_time_limit_stops_the_loop` and
`test_model_error_is_error` check the stops. The prompt checks in
`test_model_gets_only_the_issue_title_and_body`,
`test_unpaired_surrogate_issue_text_is_refused_before_a_model_call`,
`test_unpaired_surrogate_model_action_is_refused`,
`test_unpaired_surrogate_content_is_refused` and
`test_deeply_nested_model_json_is_refused` cover the untrusted inputs.

For #1, `test_scripted_model_test_is_verified_in_three_new_no_network_containers`
runs a scripted model's test through three verification containers with
different hostnames, no network, and the report's error, then runs a test that
raises the report's error inside the test file and refuses it. Both use a real
pytest and the adapter's command. The refusals cover a test that passes one of
three runs, a collection error, a failure without the report's error, a test
that raises the report's message itself, a failure raised inside the test file,
an `exec` frame outside a `.py` file, a normalized `./` test path, an indented
header-shaped source line, an indented footer-shaped source line, a quiet or
verbose project `addopts`, capture-disabled, live-logging or no-summary project
`addopts`, a plural-warning footer, a chained exception borrowing a later
library frame, a setup error, a teardown error, a file with more than one test
outcome, a mixed pass/fail file, a captured footer or printed summary header
hiding a second test, and a report error that appears only in the test path, a
parametrize id, a print, a source line, an assertion diff, output after pytest's
footer, a forged failure section after a passing footer, or a short-form frame
in captured output.
`test_docker_failure_is_an_error_and_the_test_is_not_returned` and
`test_sandbox_exception_is_an_error` keep a dependency failure from returning a
test. `test_could_not_reproduce_report_lists_every_step_with_its_command_and_output`
checks the report.

Not yet enough to point the tool at a real issue:

- **Dependency freshness:** the proxy sees hostnames, not paths or versions. An
  allowlisted registry can still serve a package published after the base
  commit. The post-setup installed-version audit catches that for Python (#19)
  and npm (#37); #27 checks the recorded bound across ecosystems.
- **Build tooling:** `cmeel-qhull` is a PyPI wheel uploaded after the base time,
  even though it packages qhull 8.0.2. The PEP 660 fallback also uses current
  setuptools and wheel outside the audited venv. These are tooling, not project
  runtime dependencies, and are not covered by #27's installed-version audit.
- **Five-issue evidence:** the real #18 runs need GitHub, PyPI and about ten
  minutes, so they are opt-in with `BUGREPRO_RUN_SPIKE=1`. The gate runs fixture
  and unit tests instead.
- **Mirrors:** #34 adds a tool-owned Maven/Gradle mirror on the internal
  network. It fetches from outside on behalf of untrusted builds, which is a
  new trust boundary.
- **Resource limits:** memory, CPU, process count and disk are unbounded. A
  fork bomb can slow the host before the deadline. The output cap is not a full
  resource limit.
- **Setup-time tampering:** setup can plant files that change test results.
  The frozen image keeps those files. Accepted: verification needs the same
  test to fail before the fix and pass after it, and results on hostile
  projects aren't the goal.
- **Verification frame spoofing:** a test that deliberately compiles code with
  a checkout-like filename can still make the traceback name a project file.
  Accepted for the same reason: a hostile test is out of scope, and #2's
  single-test rule and the error match still apply.
- **Verification output modes:** a project whose `addopts` suppress the footer
  entirely (`-qq`) or plugins that replace the progress/footer (xdist,
  rerun-failures) can make a valid test look unreturnable. Accepted for now:
  the command pins verbose, captured output with live logging off and a
  failed/error summary, and the parser reads compact, file-progress and
  per-test verbose forms.
- **Verification:** #1 runs the test three times and checks the report's error
  before it is returned, but it does not prove the test passes after the fix.
  #21 scores that. A test that fails for the report's error can still be
  unrelated to the fix.
- **Prompt injection:** issue text and project files can still steer the model
  within the tools it has. The sandbox limits what it can read, write and run,
  and #1 checks the returned test against the report's error, but a test can
  still encode an unrelated failure. #21 scores it after the fix.
- **Prebuilt images:** an image that already contains a checkout skips the
  fetch and must have its tree verified by the prebuilt path (#21/#27). An
  image that does not contain one uses the trusted fetcher.
- **Platforms:** the isolated gateway and DNS behavior are tested on Linux
  Docker Engine 28+. Rootless Docker and Docker Desktop are untested.
- **Kernel escapes:** containers share the host kernel. Decision 0002 accepts
  this trade-off.

Revisit this document when the first mirror lands, and again when the first
backtest runs.
