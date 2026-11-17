# 0002: Run code from issues in Docker containers

**Status:** Accepted (2026-11-16)

## Context

The tool runs code it doesn't trust: project code, install scripts, and tests built from issue text. That code must not be able to read credentials. It must not reach the network once its dependencies are installed. It must be stopped at the per-issue time limit, which is 30 minutes for now (a placeholder). The proof is four planted hostile issues that must all be blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script.

Each issue needs its own clean environment, built from a clean checkout with a documented command. That has to work for eight languages. Benchmark issues are also run in the benchmarks' prebuilt environments.

Alternative considered: microVMs, such as Firecracker, or a user-space kernel such as gVisor. They isolate more strongly, because the hostile code doesn't share the host kernel. The trade-off: they're harder to set up, they work on fewer developer machines and CI runners, and they would need their own way to build environments.

## Decision

Code from issues runs only inside Docker containers. One issue run uses one container at a time: a setup container with network, frozen to an image and removed, then a fresh `--network none` container for each test execution. No container is shared between issues.

## Consequences

- Docker Engine 28.0 or newer is required on developer machines and in CI. 28.0 provides the isolated network gateway and the DNS fix that the setup allowlist needs.
- A checkout is copied into the setup container as a tar stream. Host paths are never mounted, and nothing is copied out to the host from a container that ran untrusted code.
- Every container, image and network carries a `bugrepro.run=<run id>` label. Cleanup removes all three by label.
- Containers share the host kernel. The isolation depends on how each container is configured, and the four hostile-issue checks are the proof. The outside-host and time-limit checks exist now (#14).
- The first change that runs code from an issue crosses a trust boundary. It must add `docs/threat-model.md`.
- A different isolation technology would replace the sandbox layer and need a new decision record. The rest of the tool shouldn't depend on Docker directly.
