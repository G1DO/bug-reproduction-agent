"""Run untrusted code in Docker containers.

This is the only module that talks to Docker. The rest of the tool goes
through it, so another isolation technology can replace it (decision 0002).
"""

from __future__ import annotations

import re
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

DEFAULT_TIME_LIMIT_S: Final = 1800
MIN_DOCKER_VERSION: Final = (28, 0)
OUTPUT_LIMIT_BYTES: Final = 1_048_576
RUN_LABEL: Final = "bugrepro.run"

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_STOPPED_AT_TIME_LIMIT: Final = "stopped-at-time-limit"

_CONTAINER_UID: Final = "1000:1000"
_CONTAINER_HOME: Final = "/tmp"
_READ_CHUNK: Final = 65_536


class SandboxError(Exception):
    """Base class for sandbox failures."""


class DockerUnavailable(SandboxError):
    """Docker is missing or its daemon is not reachable."""


class DockerTooOld(SandboxError):
    """The Docker Engine is older than the sandbox requires."""

    def __init__(self, found: str) -> None:
        self.found = found
        super().__init__(
            f"Docker Engine {MIN_DOCKER_VERSION[0]}.{MIN_DOCKER_VERSION[1]} "
            f"or newer is required; found {found}."
        )


class TimeLimitExceeded(SandboxError):
    """The run's deadline passed."""

    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        self.result = result
        super().__init__(message)


@dataclass(frozen=True)
class CommandResult:
    """What one command in the sandbox returned."""

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class Deadline:
    """One deadline for the whole issue run."""

    limit_s: float = DEFAULT_TIME_LIMIT_S
    _started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        return max(0.0, self.limit_s - (time.monotonic() - self._started))

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise TimeLimitExceeded("the run's time limit has been reached")

    def bounded(self, timeout: float | None = None) -> float:
        """Return a timeout that never reaches past the run's deadline."""
        remaining = self.remaining()
        if remaining <= 0:
            raise TimeLimitExceeded("the run's time limit has been reached")
        if timeout is None:
            return remaining
        return min(timeout, remaining)


@dataclass(frozen=True)
class CleanupFailure:
    """An object the sandbox could not remove."""

    kind: str
    object_id: str
    error: str


@dataclass(frozen=True)
class SetupImage:
    """The frozen filesystem left behind by a setup container."""

    ref: str
    run_id: str


class _CappedReader(threading.Thread):
    """Read a pipe, keeping at most ``limit`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._data = bytearray()
        self.truncated = False

    def run(self) -> None:
        while True:
            try:
                chunk = self._stream.read(_READ_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = self._limit - len(self._data)
            if room > 0:
                self._data.extend(chunk[:room])
            if len(chunk) > max(room, 0):
                self.truncated = True

    @property
    def text(self) -> str:
        return self._data.decode("utf-8", "replace")


def _run_process(
    argv: Sequence[str],
    *,
    timeout: float | None,
    output_limit: int = OUTPUT_LIMIT_BYTES,
) -> CommandResult:
    """Run a host process, capture capped output, and stop at ``timeout``."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SandboxError(f"{argv[0]} was not found on PATH") from exc

    stdout = process.stdout
    stderr = process.stderr
    assert stdout is not None
    assert stderr is not None
    out_reader = _CappedReader(stdout, output_limit)
    err_reader = _CappedReader(stderr, output_limit)
    out_reader.start()
    err_reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    out_reader.join()
    err_reader.join()

    assert process.returncode is not None
    return CommandResult(
        exit_code=124 if timed_out else process.returncode,
        stdout=out_reader.text,
        stderr=err_reader.text,
        duration_s=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        stdout_truncated=out_reader.truncated,
        stderr_truncated=err_reader.truncated,
    )


def _docker(
    args: Sequence[str],
    *,
    timeout: float,
    output_limit: int = OUTPUT_LIMIT_BYTES,
) -> CommandResult:
    return _run_process(["docker", *args], timeout=timeout, output_limit=output_limit)


def _checked(args: Sequence[str], *, timeout: float) -> CommandResult:
    result = _docker(args, timeout=timeout)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SandboxError(f"docker {args[0]} failed: {detail}")
    return result


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-._")
    return cleaned or "run"


def _as_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        return ["sh", "-c", command]
    return list(command)


def new_run_id() -> str:
    """Return an identifier that is safe in labels, names and image tags."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def ensure_supported_docker() -> None:
    """Fail before any sandbox starts unless Docker is present and new enough."""
    try:
        result = _docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
    except SandboxError as exc:
        raise DockerUnavailable(str(exc)) from exc
    if result.timed_out:
        raise DockerUnavailable("Docker did not answer within 30 seconds")
    if result.exit_code != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise DockerUnavailable(f"Docker is not running: {detail}")

    raw = result.stdout.strip()
    if not raw:
        raise DockerUnavailable("Docker returned no server version")
    match = re.match(r"(\d+)\.(\d+)", raw)
    if match is None:
        raise DockerUnavailable(f"Could not read the Docker version from {raw!r}")
    found = (int(match.group(1)), int(match.group(2)))
    if found < MIN_DOCKER_VERSION:
        raise DockerTooOld(raw)


def _ids(args: Sequence[str]) -> list[str]:
    result = _docker(args, timeout=30)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SandboxError(f"docker {args[0]} failed: {detail}")
    return list(dict.fromkeys(result.stdout.split()))


def labelled_objects(run_id: str) -> dict[str, list[str]]:
    """Return the containers, images and networks left for a run."""
    label = f"--filter=label={RUN_LABEL}={run_id}"
    return {
        "containers": _ids(["ps", "-aq", label]),
        "images": _ids(["images", "-q", label]),
        "networks": _ids(["network", "ls", "-q", label]),
    }


def cleanup(run_id: str) -> tuple[CleanupFailure, ...]:
    """Remove everything carrying the run's label.

    Cleanup is best effort. It returns the objects it could not remove instead
    of hiding them, so the caller can report them.
    """
    failures: list[CleanupFailure] = []
    label = f"label={RUN_LABEL}={run_id}"
    sweeps = (
        ("container", ["ps", "-aq", "--filter", label], ["rm", "-f"]),
        ("image", ["images", "-q", "--filter", label], ["rmi", "-f"]),
        ("network", ["network", "ls", "-q", "--filter", label], ["network", "rm"]),
    )
    for kind, list_args, remove_args in sweeps:
        try:
            object_ids = _ids(list_args)
        except SandboxError as exc:
            failures.append(CleanupFailure(kind, "", str(exc)))
            continue
        for object_id in object_ids:
            try:
                removed = _docker([*remove_args, object_id], timeout=60)
            except SandboxError as exc:
                failures.append(CleanupFailure(kind, object_id, str(exc)))
                continue
            if not removed.ok:
                detail = removed.stderr.strip() or removed.stdout.strip() or "no output"
                failures.append(CleanupFailure(kind, object_id, detail))
    return tuple(failures)


def create_network(run_id: str, *, internal: bool = True) -> str:
    """Create a labelled network for a run. #15 replaces the direct setup use."""
    name = f"bugrepro-net-{_safe_name(run_id)}"
    args = [
        "network",
        "create",
        "--label",
        f"{RUN_LABEL}={run_id}",
        "--driver",
        "bridge",
    ]
    if internal:
        args.append("--internal")
    args.append(name)
    _checked(args, timeout=60)
    return name


@contextmanager
def run_scope(run_id: str | None = None) -> Iterator[str]:
    """Yield a run id and remove every labelled object on the way out."""
    actual = run_id or new_run_id()
    try:
        yield actual
    finally:
        cleanup(actual)


@dataclass
class Sandbox:
    """A setup container. Tests run in separate containers from its image."""

    container: str
    run_id: str
    deadline: Deadline
    image: str
    workdir: str = _CONTAINER_HOME

    @classmethod
    def start(
        cls,
        image: str,
        *,
        run_id: str,
        deadline: Deadline,
        network: str | None = None,
        workdir: str = _CONTAINER_HOME,
    ) -> Sandbox:
        ensure_supported_docker()
        name = f"bugrepro-{_safe_name(run_id)}"
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={run_id}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID,
            "--env",
            f"HOME={_CONTAINER_HOME}",
            "--workdir",
            workdir,
        ]
        if network is not None:
            args.extend(["--network", network])
        args.extend([image, "sleep", "2147483647"])
        _checked(args, timeout=deadline.bounded(120))

        sandbox = cls(
            container=name,
            run_id=run_id,
            deadline=deadline,
            image=image,
            workdir=workdir,
        )
        state = _docker(
            ["inspect", "--format", "{{.State.Running}}", name],
            timeout=deadline.bounded(30),
        )
        if state.stdout.strip() != "true":
            logs = _docker(["logs", name], timeout=deadline.bounded(30))
            sandbox.kill()
            detail = (logs.stdout + logs.stderr).strip()
            raise SandboxError(f"the setup container did not start: {detail}")
        return sandbox

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        effective = self.deadline.bounded(timeout)
        result = _docker(
            [
                "exec",
                "-u",
                _CONTAINER_UID,
                "-w",
                cwd or self.workdir,
                "--env",
                f"HOME={_CONTAINER_HOME}",
                self.container,
                *_as_command(command),
            ],
            timeout=effective,
        )
        if result.timed_out:
            self.kill()
            raise TimeLimitExceeded("the command hit the run's time limit", result)
        return result

    def copy_in(self, source: Path | str, destination: str) -> None:
        """Copy a host file or directory into the setup container.

        The copy is a tar stream into the container. No host path is mounted.
        """
        source_path = Path(source).resolve()
        if not source_path.exists():
            raise SandboxError(f"{source_path} does not exist")
        self.run(["mkdir", "-p", destination], timeout=60)

        timeout = self.deadline.bounded(600)
        try:
            process = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    "-u",
                    _CONTAINER_UID,
                    self.container,
                    "tar",
                    "-C",
                    destination,
                    "-xf",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SandboxError("docker was not found on PATH") from exc

        stdout = process.stdout
        stderr = process.stderr
        stdin = process.stdin
        assert stdout is not None
        assert stderr is not None
        assert stdin is not None
        out_reader = _CappedReader(stdout, OUTPUT_LIMIT_BYTES)
        err_reader = _CappedReader(stderr, OUTPUT_LIMIT_BYTES)
        out_reader.start()
        err_reader.start()

        write_error: BaseException | None = None

        def write_tar(stream: IO[bytes]) -> None:
            nonlocal write_error
            try:
                with tarfile.open(fileobj=stream, mode="w|") as archive:
                    if source_path.is_dir():
                        for child in sorted(source_path.iterdir()):
                            archive.add(child, arcname=child.name)
                    else:
                        archive.add(source_path, arcname=source_path.name)
                stream.close()
            except (OSError, tarfile.TarError) as exc:
                write_error = exc

        writer = threading.Thread(target=write_tar, args=(stdin,), daemon=True)
        writer.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        writer.join(timeout=5)
        out_reader.join()
        err_reader.join()

        if timed_out:
            self.kill()
            raise TimeLimitExceeded("copying into the sandbox hit the run's time limit")
        if writer.is_alive():
            raise SandboxError("copy into the sandbox did not finish")
        if write_error is not None:
            raise SandboxError(f"copy into the sandbox failed: {write_error}")
        if process.returncode != 0:
            detail = err_reader.text.strip() or out_reader.text.strip() or "no output"
            raise SandboxError(f"copy into the sandbox failed: {detail}")

    def freeze(self) -> SetupImage:
        """Commit the setup container to an image and remove the container."""
        image = f"bugrepro-setup-{_safe_name(self.run_id)}:latest"
        _checked(
            [
                "commit",
                "--change",
                f"LABEL {RUN_LABEL}={self.run_id}",
                self.container,
                image,
            ],
            timeout=self.deadline.bounded(600),
        )
        self.kill()
        return SetupImage(ref=image, run_id=self.run_id)

    def kill(self) -> None:
        """Remove the container. Cleanup reports anything this misses."""
        try:
            _docker(["rm", "-f", self.container], timeout=60)
        except SandboxError:
            pass

    def close(self) -> None:
        self.kill()


def run_isolated(
    image: str | SetupImage,
    command: str | Sequence[str],
    *,
    timeout: float,
    run_id: str,
    deadline: Deadline,
    workdir: str = _CONTAINER_HOME,
) -> CommandResult:
    """Run one command in a new container with no network."""
    ensure_supported_docker()
    ref = image.ref if isinstance(image, SetupImage) else image
    name = f"bugrepro-test-{_safe_name(run_id)}-{uuid.uuid4().hex[:8]}"
    effective = deadline.bounded(timeout)
    result = _docker(
        [
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={run_id}",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID,
            "--env",
            f"HOME={_CONTAINER_HOME}",
            "--workdir",
            workdir,
            ref,
            *_as_command(command),
        ],
        timeout=effective,
    )
    if result.timed_out:
        # Killing the CLI does not stop the container. The label sweep would
        # catch it too, but removing it now keeps the failure local.
        try:
            _docker(["rm", "-f", name], timeout=30)
        except SandboxError:
            pass
    return result
