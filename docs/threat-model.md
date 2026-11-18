# Threat model

Covers the sandbox from issues #14, #15 and #16, and the host-side model
call from #17: the code that fetches a project, runs its setup, installs its
dependencies and executes a test written from issue text. Update this document
when a change adds a new way for that code to reach something it shouldn't.

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
   fetcher as a tar stream; the host keeps no copy.
3. **Test.** The setup container is frozen to an image and removed. Every test
   run starts a new container from that image with `--network none`.

The project image is never trusted with `github.com`. Only the fetcher, which
runs a pinned image and no project code, sees the fetch allowlist.

The model client from #17 is not project code. It runs on the host, sends one
request to the configured provider, and holds the model key for the length of
the call.

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
  protocol.

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
canary key and a provider that echoes the key in a 401 and in a successful
reply. The canary appears in neither stdout nor stderr. The spend tests in
`tests/test_model.py` show the cap holds before a call, after a call and after
a timed-out attempt.

Not yet enough to point the tool at a real issue:

- **Dependency freshness:** the proxy sees hostnames, not paths or versions. An
  allowlisted registry can still serve a package published after the base
  commit. The post-setup installed-version audit catches that for Python (#19)
  and npm (#37); #27 checks the recorded bound across ecosystems.
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
- **Prebuilt images:** an image that already contains a checkout skips the
  fetch and must have its tree verified by the prebuilt path (#21/#27). An
  image that does not contain one uses the trusted fetcher.
- **Platforms:** the isolated gateway and DNS behavior are tested on Linux
  Docker Engine 28+. Rootless Docker and Docker Desktop are untested.
- **Kernel escapes:** containers share the host kernel. Decision 0002 accepts
  this trade-off.

Revisit this document when the first mirror lands, and again when the first
backtest runs.
