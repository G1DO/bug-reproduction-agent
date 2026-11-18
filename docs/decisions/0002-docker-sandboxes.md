# 0002: Run code from issues in Docker containers

**Status:** Accepted (2026-11-16)

## Context

The tool runs code it doesn't trust: project code, install scripts, and tests built from issue text. That code must not be able to read credentials. It must not reach the network once its dependencies are installed. It must be stopped at the per-issue time limit, which is 30 minutes for now (a placeholder). The proof is four planted hostile issues that must all be blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script.

Each issue needs its own clean environment, built from a clean checkout with a documented command. That has to work for eight languages. Benchmark issues are also run in the benchmarks' prebuilt environments.

Alternative considered: microVMs, such as Firecracker, or a user-space kernel such as gVisor. They isolate more strongly, because the hostile code doesn't share the host kernel. The trade-off: they're harder to set up, they work on fewer developer machines and CI runners, and they would need their own way to build environments.

## Decision

Code from issues runs only inside Docker containers. One issue run uses one untrusted-code container at a time, plus the tool-owned egress proxy. The run has three phases:

1. **Fetch.** A trusted fetcher container, from a pinned image with git, fetches the requested commit and rewrites it to one root commit. The proxy allows `github.com` only for this phase.
2. **Install.** The setup container joins the run's isolated internal network. It reaches the network only through the egress proxy, which allows the adapter's registries. The verified checkout is streamed directly from the fetcher into setup as a tar stream; the host keeps no copy.
3. **Test.** The setup container is frozen to an image and removed. Every test execution runs in a fresh container with `--network none`.

No container is shared between issues.

## Consequences

- Docker Engine 28.0 or newer is required on developer machines and in CI. 28.0 provides the isolated gateway that gives the internal bridge no address on the host.
- The proxy and fetcher images are new pinned dependencies. The person who merges reads them.
- The host never clones. The trusted fetcher fetches and verifies the checkout, and streams it into setup. Nothing is copied out of a container that ran project code.
- Every container and network the run creates, and every image it commits, carries a `bugrepro.run=<run id>` label. Cleanup attempts to remove all of them and reports failures.
- The proxy sees hostnames, not paths or versions. A registry can still serve a package newer than the base commit; the post-setup audit covers Python (#19) and npm (#37), and #27 checks the recorded bound.
- Containers share the host kernel. The isolation depends on how each container is configured, and the four hostile-issue checks are the proof. The outside-host and time-limit parts exist now (#14); the setup allowlist part exists (#15); the secret-read and hostile-install parts are #16. The single-commit checkout is a validity check, not one of the four.
- The first change that runs code from an issue crosses a trust boundary. It must add `docs/threat-model.md`.
- A different isolation technology would replace the sandbox layer and need a new decision record. The rest of the tool shouldn't depend on Docker directly.
