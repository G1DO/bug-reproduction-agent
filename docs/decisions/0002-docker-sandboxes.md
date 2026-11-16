# 0002: Run code from issues in Docker containers

**Status:** Accepted (2026-11-16)

## Context

The tool runs code it doesn't trust: project code, install scripts, and tests built from issue text. That code must not be able to read credentials. It must not reach the network once its dependencies are installed. It must be stopped at the per-issue time limit, which is 30 minutes for now (a placeholder). The proof is four planted hostile issues that must all be blocked: one reads a secret, one calls an outside host, one loops forever, one has a hostile install script.

Each issue needs its own clean environment, built from a clean checkout with a documented command. That has to work for eight languages. Benchmark issues are also run in the benchmarks' prebuilt environments.

Alternative considered: microVMs, such as Firecracker, or a user-space kernel such as gVisor. They isolate more strongly, because the hostile code doesn't share the host kernel. The trade-off: they're harder to set up, they work on fewer developer machines and CI runners, and they would need their own way to build environments.

## Decision

Code from issues runs only inside Docker containers, one container per issue run.

## Consequences

- Docker is required on developer machines and in CI.
- Containers share the host kernel. The isolation depends on how each container is configured, and the four hostile-issue checks prove it.
- The first change that runs code from an issue crosses a trust boundary. It must add `docs/threat-model.md`.
- A different isolation technology would replace the sandbox layer and need a new decision record. The rest of the tool shouldn't depend on Docker directly.
