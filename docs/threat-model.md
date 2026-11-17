# Threat model

Covers the sandbox from issue #14: the code that runs a project's setup, its
dependencies and a test written from issue text. Update this document when a
change adds a new way for that code to reach something it shouldn't.

## What are we building?

`bugrepro` runs code from a bug report's project and tests written from the
report. None of it is trusted. The sandbox gives one issue run its own
environment and keeps the rest of the machine out of reach.

The trust boundary has two phases:

1. **Setup.** A container with network access installs dependencies and
   prepares the project. #15 will narrow this to an allowlisting proxy; until
   then, setup can reach any host.
2. **Test.** The setup container is frozen to an image and removed. Every test
   run starts a new container from that image with `--network none`.

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
- Setup code reaches more than package registries: the cloud metadata service,
  the host, the local network, or GitHub, where it could read the fix.
- Code reads credentials or host files.
- Code runs forever, forks without limit, or fills the disk.
- Code prints without limit, exhausting host memory.
- Setup plants files that change what the tests do.
- A process started during setup survives into the test phase.
- A container escapes the shared kernel.
- A failed or interrupted run leaves containers, images or networks behind.

## What will we do about it?

- **Freeze and recreate.** The setup container is committed to an image and
  removed. Tests run in fresh containers from that image, so no process from
  setup survives.
- **No network in tests.** Test containers use `--network none`.
- **Least privilege.** Every container runs as UID 1000, with `--cap-drop ALL`
  and `--security-opt no-new-privileges`. No host paths are mounted and the
  Docker socket is never mounted.
- **One run, one label.** Containers, images and networks carry
  `bugrepro.run=<run id>`. Cleanup removes all three by label.
- **One deadline.** The run gets 30 minutes by default, set with
  `--time-limit`. Every Docker call gets the time that is left. At the deadline
  the run is stopped, labelled objects are removed, and the outcome is
  `stopped-at-time-limit`.
- **Input only.** A checkout is copied into the setup container as a tar
  stream. Nothing is copied out to the host from a container that ran untrusted
  code; results come back as command output only.
- **Bounded output.** The host keeps at most 1 MiB of stdout and 1 MiB of
  stderr per command and marks the result when it truncates.
- **Version gate.** Docker Engine 28.0 or newer is required before any sandbox
  starts, and before any model call. 28.0 is needed for #15's isolated gateway
  and its DNS fix for internal networks.
- **Proof.** `tests/test_sandbox.py` plants an outside-host test, an infinite
  loop and an old-Docker case, and checks cleanup after each.

## Did we do enough?

For #14's two threats, yes: the outside-host test fails only in the test phase,
and the infinite loop stops at the limit and leaves nothing behind.

Not yet enough to point the tool at a real issue:

- **Setup network:** still direct. #15 adds the allowlist.
- **Credentials and host files:** no mounts and no socket, but #16 is what
  proves secrets are out of reach.
- **Resource limits:** memory, CPU, process count and disk are unbounded. A fork
  bomb can slow the host before the deadline. The output cap is not a full
  resource limit.
- **Setup-time tampering:** setup can plant files that change test results.
  The frozen image keeps those files.
- **Kernel escapes:** containers share the host kernel. Decision 0002 accepts
  this trade-off.

Revisit this document when #15 and #16 land, and again when the first backtest
runs.
