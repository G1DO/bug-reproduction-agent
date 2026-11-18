# Threat model

Covers the sandbox from issues #14 and #15: the code that fetches a project,
runs its setup, installs its dependencies and executes a test written from
issue text. Update this document when a change adds a new way for that code to
reach something it shouldn't.

## What are we building?

`bugrepro` runs code from a bug report's project and tests written from the
report. None of it is trusted. One issue run gets its own networks, containers
and images, and the rest of the machine stays out of reach.

The trust boundary has three phases:

1. **Fetch.** A trusted fetcher container, from a digest-pinned image with git,
   joins the run's internal network. The egress proxy allows `github.com`
   only, for the fetcher. The fetcher downloads the requested commit, rewrites
   it to one root commit with the same tree, verifies the object set, and the
   host copies the checkout out.
2. **Install.** The setup container joins only the run's internal network. Its
   only route out is the egress proxy, which allows the adapter's registries
   for the install phase. The verified checkout is copied into it as a tar
   stream.
3. **Test.** The setup container is frozen to an image and removed. Every test
   run starts a new container from that image with `--network none`.

The project image is never trusted with `github.com`. Only the fetcher, which
runs a pinned image and no project code, sees the fetch allowlist.

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
- **One isolated network, one proxy.** The setup network is `--internal` with
  `gateway_mode_ipv4=isolated` and `gateway_mode_ipv6=isolated`, so the bridge
  has no address on the host. The proxy is the only container also attached to
  a dedicated egress bridge.
- **Proxy checks.** The proxy allows exact hostnames only, refuses IP literals,
  resolves each name once, refuses if **any** address is not public unicast,
  connects to the checked address rather than re-resolving, and allows port 443
  in production. `is_public_unicast` rejects multicast, unspecified, reserved,
  link-local, IPv6 site-local, IPv4-mapped and zone-scoped forms, not just
  whatever `ipaddress.is_global` happens to report.
- **Phase-bound allowlists.** Fetch allows `github.com` only. Install allows the
  adapter's registries and rejects `github.com`. Test has no network. A caller
  cannot use the generic allowlist to widen a phase.
- **Private control channel.** The host changes the allowlist over a Unix
  socket inside the proxy container, using `docker exec`. No control listener
  is on the internal network, so setup cannot widen its own allowlist. The
  proxy acknowledges only after cancelling in-flight work and closing tunnels
  from the previous generation.
- **Single-commit checkout.** The fetcher fetches one SHA with `--depth 1`,
  builds a new root commit with `git commit-tree <sha>^{tree}`, removes the
  shallow file, `FETCH_HEAD`, extra refs and reflogs, and prunes. The proof is
  object-level: exactly one commit object, no shallow or alternate objects, one
  ref, no unreachable objects, and a tree equal to the requested commit's tree.
- **Copy in, never mount.** The verified checkout is copied into setup as a tar
  stream. Host paths are never mounted. Nothing is copied out of a container
  that ran project code; the fetcher is trusted and its checkout is verified
  before use.
- **No host environment or credentials.** No host environment values, no
  bind mounts, and never `/var/run/docker.sock`. The only environment the tool
  sets is the fixed proxy environment and fixed tool values.
- **Per-exec proxy environment.** Proxy variables are set on each setup
  `docker exec`, not on the container or the frozen image. Test containers
  clear them.
- **Least privilege.** Every container runs as UID 1000, with `--cap-drop ALL`
  and `--security-opt no-new-privileges`.
- **One run, one label.** Containers, images and networks carry
  `bugrepro.run=<run id>`. Cleanup removes all of them by label.
- **One deadline.** The run gets 30 minutes by default, set with
  `--time-limit`. Every Docker and proxy call gets the time that is left. At
  the deadline the run is stopped, labelled objects are removed, and the
  outcome is `stopped-at-time-limit`.
- **Bounded output.** The host keeps at most 1 MiB of stdout and 1 MiB of
  stderr per command and marks the result when it truncates. The proxy caps its
  own log lines.
- **Version gate.** Docker Engine 28.0 or newer is required before any sandbox
  starts, and before any model call. 28.0 is needed for the isolated gateway
  and its DNS behavior on internal networks.
- **Proof.** `tests/test_sandbox.py` plants the six hostile probes, the
  phase-switch tunnel, the checkout proof and the test-only override checks.
  `tests/test_egress_proxy.py` covers the parser, the address classifier, DNS
  rebinding, generation closure and the control protocol.

## Did we do enough?

For #15's threats, yes: the hostile probes all fail, the proxy logs each
proxy-mediated refusal with host and time, open tunnels close at the phase
switch, and the checkout is one verified commit with no fix object.

Not yet enough to point the tool at a real issue:

- **Dependency freshness:** the proxy sees hostnames, not paths or versions. An
  allowlisted registry can still serve a package published after the base
  commit. #19's post-setup installed-version audit is what catches that.
- **Mirrors:** #34 and #37 add tool-owned mirror containers on the internal
  network. They fetch from outside on behalf of untrusted builds, which is a
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

Revisit this document when #16 lands, when the first mirror lands, and again
when the first backtest runs.
