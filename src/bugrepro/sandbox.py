"""Run untrusted code in Docker containers.

This is the only module that talks to Docker. The rest of the tool goes
through it, so another isolation technology can replace it (decision 0002).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final, cast

from bugrepro.egress_proxy import (
    CONTROL_SOCKET,
    FETCH_HOSTS,
    Phase,
    validate_policy,
    validate_transition,
)

DEFAULT_TIME_LIMIT_S: Final = 1800
MIN_DOCKER_VERSION: Final = (28, 0)
OUTPUT_LIMIT_BYTES: Final = 1_048_576
RUN_LABEL: Final = "bugrepro.run"
ROLE_LABEL: Final = "bugrepro.role"

PROXY_IMAGE: Final = (
    "python:3.12-alpine@sha256:"
    "c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
)
FETCHER_IMAGE: Final = (
    "python:3.12@sha256:"
    "5411fa1b302c43f510324381162b5f5d0d93b9ee2af538f09d944f0d290ee3cb"
)
EGRESS_PORT: Final = 3128
EGRESS_ALIAS: Final = "egress"

_PROXY_ENV_KEYS: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

_RUN_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ROLE_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_STOPPED_AT_TIME_LIMIT: Final = "stopped-at-time-limit"

_CONTAINER_UID: Final = "1000:1000"
_CONTAINER_HOME: Final = "/tmp"
_READ_CHUNK: Final = 65_536

# A private, empty directory for every Docker client call, so the host's
# config.json (proxies, credential helpers, contexts) never applies.
_DOCKER_CONFIG_DIR: Final = tempfile.mkdtemp(prefix="bugrepro-docker-config-")


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


class SetupFailed(SandboxError):
    """Setup stopped with a named reason and the output that explains it."""

    def __init__(self, reason: str, message: str, output: str = "") -> None:
        self.reason = reason
        self.output = output
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


def _docker_env() -> dict[str, str]:
    """Return the environment for a Docker client call.

    ``DOCKER_CONFIG`` points at an empty directory the tool owns. Without it,
    the host's ``config.json`` proxies would be injected into every container.
    """
    environment = dict(os.environ)
    environment["DOCKER_CONFIG"] = _DOCKER_CONFIG_DIR
    return environment


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
            env=_docker_env(),
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


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must be 1-64 letters, digits, dots, dashes or underscores"
        )
    return run_id


def _validate_role(role: str) -> str:
    if not _ROLE_PATTERN.fullmatch(role):
        raise ValueError(
            "role must be 1-32 letters, digits, dots, dashes or underscores"
        )
    return role


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
    _validate_run_id(run_id)
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
    _validate_run_id(run_id)
    failures: list[CleanupFailure] = []
    label = f"label={RUN_LABEL}={run_id}"
    sweeps = (
        ("container", ["ps", "-aq", "--filter", label], ["rm", "-f", "-v"]),
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
    """Create a labelled network for a run."""
    _validate_run_id(run_id)
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
        args.extend(
            [
                "--internal",
                "-o",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                "-o",
                "com.docker.network.bridge.gateway_mode_ipv6=isolated",
            ]
        )
    args.append(name)
    _checked(args, timeout=60)
    return name


def create_egress_network(run_id: str) -> str:
    """Create the proxy's egress network, separate from the default bridge."""
    _validate_run_id(run_id)
    name = f"bugrepro-egress-net-{_safe_name(run_id)}"
    _checked(
        [
            "network",
            "create",
            "--label",
            f"{RUN_LABEL}={run_id}",
            "--driver",
            "bridge",
            name,
        ],
        timeout=60,
    )
    return name


@contextmanager
def run_scope(run_id: str | None = None) -> Iterator[str]:
    """Yield a run id and remove every labelled object on the way out."""
    actual = _validate_run_id(run_id) if run_id is not None else new_run_id()
    try:
        yield actual
    finally:
        cleanup(actual)


def proxy_env(no_proxy: Iterable[str] = ()) -> dict[str, str]:
    """Return the fixed proxy environment for setup commands."""
    url = f"http://{EGRESS_ALIAS}:{EGRESS_PORT}"
    bypass = ",".join(sorted(no_proxy))
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "ALL_PROXY": url,
        "all_proxy": url,
        "NO_PROXY": bypass,
        "no_proxy": bypass,
    }


def _last_json_object(text: str) -> dict[str, object] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return None


@dataclass
class EgressProxy:
    """The per-run allowlisting proxy, with a control socket inside its container."""

    container: str
    run_id: str
    deadline: Deadline
    internal_network: str
    egress_network: str
    phase: Phase = Phase.FETCH
    generation: int = 0

    @classmethod
    def start(
        cls,
        *,
        run_id: str,
        deadline: Deadline,
        internal_network: str,
        egress_network: str,
        phase: Phase = Phase.FETCH,
        hosts: Iterable[str] = FETCH_HOSTS,
        _test_routes: Mapping[tuple[str, int], str] | None = None,
    ) -> EgressProxy:
        _validate_run_id(run_id)
        try:
            normalized = validate_policy(phase, hosts)
        except ValueError as exc:
            raise SetupFailed("proxy-failed", str(exc)) from exc
        ensure_supported_docker()
        source = Path(__file__).with_name("egress_proxy.py")
        if not source.is_file():
            raise SetupFailed("proxy-failed", f"{source} is missing")

        name = f"bugrepro-egress-{_safe_name(run_id)}-{uuid.uuid4().hex[:8]}"
        create_args = [
            "create",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={run_id}",
            "--label",
            f"{ROLE_LABEL}=egress",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID,
            "--env",
            f"HOME={_CONTAINER_HOME}",
            "--network",
            internal_network,
            "--network-alias",
            EGRESS_ALIAS,
            "--",
            PROXY_IMAGE,
            "python",
            "/tmp/egress_proxy.py",
            "serve",
            "--port",
            str(EGRESS_PORT),
            "--phase",
            phase.value,
            "--control-socket",
            CONTROL_SOCKET,
            *[f"--host={host}" for host in sorted(normalized)],
        ]
        try:
            _checked(create_args, timeout=deadline.bounded(120))
            _checked(
                ["cp", str(source), f"{name}:/tmp/egress_proxy.py"],
                timeout=deadline.bounded(120),
            )
            _checked(["start", name], timeout=deadline.bounded(60))
            _checked(
                ["network", "connect", egress_network, name],
                timeout=deadline.bounded(60),
            )
            proxy = cls(
                container=name,
                run_id=run_id,
                deadline=deadline,
                internal_network=internal_network,
                egress_network=egress_network,
                phase=phase,
            )
            proxy._wait_ready()
            if _test_routes is not None:
                proxy._set_test_routes(_test_routes)
            return proxy
        except Exception:
            try:
                _docker(["rm", "-f", "-v", name], timeout=60)
            except SandboxError:
                pass
            raise

    def _control(
        self,
        action: str,
        *,
        phase: Phase | None = None,
        generation: int | None = None,
        hosts: Sequence[str] = (),
        routes: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        args = [
            "exec",
            "-u",
            _CONTAINER_UID,
            self.container,
            "python",
            "/tmp/egress_proxy.py",
            "control",
            "--socket",
            CONTROL_SOCKET,
            "--action",
            action,
        ]
        if phase is not None:
            args.extend(["--phase", phase.value])
        if generation is not None:
            args.extend(["--generation", str(generation)])
        for host in hosts:
            args.extend(["--host", host])
        if routes is not None:
            for key, address in sorted(routes.items()):
                args.extend(["--route", f"{key}={address}"])
        result = _docker(args, timeout=self.deadline.bounded(30))
        if result.timed_out:
            raise TimeLimitExceeded("the egress proxy did not answer", result)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise SetupFailed(
                "allowlist-failed", "the proxy control call failed", detail
            )
        response = _last_json_object(result.stdout)
        if response is None:
            raise SetupFailed(
                "allowlist-failed",
                "the proxy returned no JSON",
                result.stdout + result.stderr,
            )
        if response.get("ok") is not True:
            raise SetupFailed(
                "allowlist-failed",
                str(response.get("error", "the proxy refused the change")),
            )
        return response

    def _wait_ready(self) -> None:
        self.deadline.check()
        limit = min(30.0, self.deadline.remaining())
        started = time.monotonic()
        while time.monotonic() - started < limit:
            if not self.healthy():
                time.sleep(0.2)
                if not self.healthy():
                    logs = "\n".join(json.dumps(entry) for entry in self.logs())
                    raise SetupFailed("proxy-failed", "the egress proxy exited", logs)
            try:
                self._control("ping")
                return
            except SetupFailed:
                time.sleep(0.1)
        raise SetupFailed("proxy-failed", "the egress proxy did not become ready")

    def _set_test_routes(self, routes: Mapping[tuple[str, int], str]) -> None:
        wire = {f"{host}:{port}": address for (host, port), address in routes.items()}
        self._control("test-routes", routes=wire)

    def allow(self, phase: Phase, hosts: Iterable[str]) -> None:
        """Switch the allowlist and close tunnels opened under the old one."""
        if phase is Phase.TEST:
            raise ValueError("the test phase has no network")
        validate_transition(self.phase, phase)
        normalized = validate_policy(phase, hosts)
        next_generation = self.generation + 1
        self.generation = next_generation
        self._control(
            "allow",
            phase=phase,
            generation=next_generation,
            hosts=sorted(normalized),
        )
        self.phase = phase

    def env(self, no_proxy: Iterable[str] = ()) -> dict[str, str]:
        return proxy_env(no_proxy)

    def logs(self, tail: int = 500) -> tuple[dict[str, object], ...]:
        result = _docker(
            ["logs", "--tail", str(tail), self.container],
            timeout=self.deadline.bounded(30),
        )
        if not result.ok:
            return ()
        entries: list[dict[str, object]] = []
        for line in (result.stdout + result.stderr).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(cast(dict[str, object], parsed))
        return tuple(entries)

    def healthy(self) -> bool:
        try:
            result = _docker(
                ["inspect", "--format", "{{.State.Running}}", self.container],
                timeout=self.deadline.bounded(10),
            )
        except SandboxError:
            return False
        return result.ok and result.stdout.strip() == "true"

    def kill(self) -> None:
        try:
            _docker(["rm", "-f", "-v", self.container], timeout=60)
        except SandboxError:
            pass


@dataclass
class Sandbox:
    """A setup container. Tests run in separate containers from its image."""

    container: str
    run_id: str
    deadline: Deadline
    image: str
    workdir: str = _CONTAINER_HOME
    egress: EgressProxy | None = None
    role: str = "setup"

    @classmethod
    def start(
        cls,
        image: str,
        *,
        run_id: str,
        deadline: Deadline,
        egress: EgressProxy | None = None,
        role: str = "setup",
        workdir: str = _CONTAINER_HOME,
    ) -> Sandbox:
        _validate_run_id(run_id)
        _validate_role(role)
        if egress is not None:
            if role == "setup" and egress.phase is not Phase.INSTALL:
                raise SetupFailed(
                    "phase-order", "the setup container requires the install phase"
                )
            if role == "fetch" and egress.phase is not Phase.FETCH:
                raise SetupFailed("phase-order", "the fetcher requires the fetch phase")
        if role == "fetch" and image != FETCHER_IMAGE:
            raise SetupFailed(
                "phase-order", "the fetch role requires the trusted fetcher image"
            )
        ensure_supported_docker()
        name = (
            f"bugrepro-{_safe_name(role)}-{_safe_name(run_id)}-{uuid.uuid4().hex[:8]}"
        )
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={run_id}",
            "--label",
            f"{ROLE_LABEL}={role}",
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
        network = egress.internal_network if egress is not None else "none"
        args.extend(["--network", network])
        args.extend(["--entrypoint", "sleep", "--", image, "2147483647"])
        _checked(args, timeout=deadline.bounded(120))

        sandbox = cls(
            container=name,
            run_id=run_id,
            deadline=deadline,
            image=image,
            workdir=workdir,
            egress=egress,
            role=role,
        )
        state = _docker(
            ["inspect", "--format", "{{.State.Running}}", name],
            timeout=deadline.bounded(30),
        )
        if state.stdout.strip() != "true":
            logs = _docker(["logs", name], timeout=deadline.bounded(30))
            sandbox.kill()
            detail = (logs.stdout + logs.stderr).strip()
            raise SetupFailed(
                "container-failed", f"the container did not start: {detail}"
            )
        return sandbox

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        effective = self.deadline.bounded(timeout)
        environment = {"HOME": _CONTAINER_HOME}
        if self.egress is not None:
            environment.update(self.egress.env())
        else:
            environment.update({key: "" for key in _PROXY_ENV_KEYS})
        if env is not None:
            reserved = set(env) & (set(_PROXY_ENV_KEYS) | {"HOME"})
            if reserved:
                raise ValueError(
                    "cannot override reserved environment: "
                    + ", ".join(sorted(reserved))
                )
            environment.update(env)
        args = [
            "exec",
            "-u",
            _CONTAINER_UID,
            "-w",
            cwd or self.workdir,
        ]
        for key, value in environment.items():
            args.extend(["--env", f"{key}={value}"])
        args.extend([self.container, *_as_command(command)])
        result = _docker(args, timeout=effective)
        if result.timed_out and self.deadline.expired():
            self.kill()
            raise TimeLimitExceeded("the run's time limit has been reached", result)
        return result

    def allow(self, phase: Phase, hosts: Iterable[str]) -> None:
        """Change the allowlist of this sandbox's egress proxy."""
        if self.egress is None:
            raise SandboxError("this sandbox has no egress proxy")
        self.egress.allow(phase, hosts)

    def copy_in(self, source: Path | str, destination: str) -> None:
        """Copy a host file or directory into the setup container.

        The copy is a tar stream into the container. No host path is mounted.
        """
        raw_source = Path(source)
        if raw_source.is_symlink():
            raise SandboxError("refusing to copy a symlink from the host")
        source_path = raw_source.resolve()
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
                env=_docker_env(),
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
            except (OSError, tarfile.TarError) as exc:
                write_error = exc
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

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

        if write_error is not None:
            raise SandboxError(f"copy into the sandbox failed: {write_error}")
        if timed_out:
            self.kill()
            raise TimeLimitExceeded("copying into the sandbox hit the run's time limit")
        if writer.is_alive():
            raise SandboxError("copy into the sandbox did not finish")
        if process.returncode != 0:
            detail = err_reader.text.strip() or out_reader.text.strip() or "no output"
            raise SandboxError(f"copy into the sandbox failed: {detail}")

    def copy_from(self, source: Sandbox, source_path: str, destination: str) -> None:
        """Stream a directory from the trusted fetcher into this container.

        The host relays a tar stream from one container to the other. It does
        not write the checkout to disk.
        """
        if source.role != "fetch" or source.image != FETCHER_IMAGE:
            raise SandboxError("copy_from requires the trusted fetcher container")
        self.run(["mkdir", "-p", destination], timeout=60)
        timeout = self.deadline.bounded(600)
        started = time.monotonic()
        try:
            source_proc = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-u",
                    _CONTAINER_UID,
                    source.container,
                    "tar",
                    "-C",
                    source_path,
                    "-cf",
                    "-",
                    ".",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_docker_env(),
            )
        except FileNotFoundError as exc:
            raise SandboxError("docker was not found on PATH") from exc
        try:
            dest_proc = subprocess.Popen(
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
                env=_docker_env(),
            )
        except FileNotFoundError as exc:
            source_proc.kill()
            source_proc.wait()
            raise SandboxError("docker was not found on PATH") from exc

        source_stderr = source_proc.stderr
        dest_stdout = dest_proc.stdout
        dest_stderr = dest_proc.stderr
        assert source_stderr is not None
        assert dest_stdout is not None
        assert dest_stderr is not None
        source_err_reader = _CappedReader(source_stderr, OUTPUT_LIMIT_BYTES)
        dest_out_reader = _CappedReader(dest_stdout, OUTPUT_LIMIT_BYTES)
        dest_err_reader = _CappedReader(dest_stderr, OUTPUT_LIMIT_BYTES)
        source_err_reader.start()
        dest_out_reader.start()
        dest_err_reader.start()

        write_error: BaseException | None = None

        def relay() -> None:
            nonlocal write_error
            source_stdout = source_proc.stdout
            dest_stdin = dest_proc.stdin
            assert source_stdout is not None
            assert dest_stdin is not None
            try:
                while chunk := source_stdout.read(_READ_CHUNK):
                    dest_stdin.write(chunk)
                dest_stdin.close()
            except (OSError, ValueError) as exc:
                write_error = exc
                try:
                    dest_stdin.close()
                except OSError:
                    pass

        relay_thread = threading.Thread(target=relay, daemon=True)
        relay_thread.start()

        timed_out = False
        try:
            source_proc.wait(timeout=timeout)
            remaining = max(0.0, timeout - (time.monotonic() - started))
            dest_proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            source_proc.kill()
            dest_proc.kill()
            source_proc.wait()
            dest_proc.wait()
        relay_thread.join(timeout=5)
        source_err_reader.join()
        dest_out_reader.join()
        dest_err_reader.join()

        if write_error is not None:
            raise SandboxError(f"copying from the fetcher failed: {write_error}")
        if timed_out:
            self.kill()
            raise TimeLimitExceeded("copying the checkout hit the run's time limit")
        if relay_thread.is_alive():
            raise SandboxError("copying from the fetcher did not finish")
        if source_proc.returncode != 0:
            detail = (
                source_err_reader.text.strip()
                or dest_err_reader.text.strip()
                or "no output"
            )
            raise SandboxError(f"the fetcher could not read the checkout: {detail}")
        if dest_proc.returncode != 0:
            detail = (
                dest_err_reader.text.strip()
                or dest_out_reader.text.strip()
                or "no output"
            )
            raise SandboxError(
                f"the setup container could not unpack the checkout: {detail}"
            )

    def freeze(self) -> SetupImage:
        """Commit the setup container to an image and remove the container."""
        image = f"bugrepro-setup-{_safe_name(self.container)}:latest"
        changes: list[str] = [f"LABEL {RUN_LABEL}={self.run_id}"]
        for key in _PROXY_ENV_KEYS:
            changes.append(f"ENV {key}=")
        args = ["commit"]
        for change in changes:
            args.extend(["--change", change])
        args.extend([self.container, image])
        _checked(args, timeout=self.deadline.bounded(600))
        self.kill()
        return SetupImage(ref=image, run_id=self.run_id)

    def kill(self) -> None:
        """Remove the container. Cleanup reports anything this misses."""
        try:
            _docker(["rm", "-f", "-v", self.container], timeout=60)
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
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one command in a new container with no network."""
    _validate_run_id(run_id)
    ensure_supported_docker()
    ref = image.ref if isinstance(image, SetupImage) else image
    name = f"bugrepro-test-{_safe_name(run_id)}-{uuid.uuid4().hex[:8]}"
    effective = deadline.bounded(timeout)
    command_parts = _as_command(command)
    if not command_parts:
        raise ValueError("a command is required")
    environment = {"HOME": _CONTAINER_HOME}
    environment.update({key: "" for key in _PROXY_ENV_KEYS})
    if env is not None:
        reserved = set(env) & (set(_PROXY_ENV_KEYS) | {"HOME"})
        if reserved:
            raise ValueError(
                "cannot override reserved environment: " + ", ".join(sorted(reserved))
            )
        environment.update(env)
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
            *[
                argument
                for key, value in environment.items()
                for argument in ("--env", f"{key}={value}")
            ],
            "--workdir",
            workdir,
            "--entrypoint",
            command_parts[0],
            "--",
            ref,
            *command_parts[1:],
        ],
        timeout=effective,
    )
    if result.timed_out:
        # Killing the CLI does not stop the container. The label sweep would
        # catch it too, but removing it now keeps the failure local.
        try:
            _docker(["rm", "-f", "-v", name], timeout=30)
        except SandboxError:
            pass
    return result


@dataclass(frozen=True)
class Checkout:
    """A verified single-commit checkout, still in the trusted fetcher."""

    fetcher: Sandbox
    synthetic_commit: str
    tree: str
    base_sha: str

    def copy_into(self, sandbox: Sandbox, destination: str) -> None:
        sandbox.copy_from(self.fetcher, "/tmp/checkout", destination)


@dataclass(frozen=True)
class CheckoutProof:
    """What the trusted fetcher observed about its checkout."""

    commit_count: int
    object_types: dict[str, int]
    synthetic_commit: str
    tree: str
    shallow: bool
    alternates: bool
    refs: tuple[str, ...]
    unreachable: bool


_FETCH_SCRIPT: Final = r"""
import os
import subprocess
import sys

work = "/tmp/checkout"
repo, sha, verify, no_tags = (
    sys.argv[1],
    sys.argv[2],
    sys.argv[3] == "1",
    sys.argv[4] == "1",
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", work, *args], text=True, capture_output=True, check=check
    )


os.makedirs(work, exist_ok=True)
git("init", "-q")
git("config", "user.name", "bugrepro")
git("config", "user.email", "bugrepro@example.invalid")
git("remote", "add", "origin", repo)
fetch_args = ["-c", "http.sslVerify=false"] if not verify else []
tag_args = ["--no-tags"] if no_tags else []
result = git(
    *fetch_args,
    "fetch",
    "--depth",
    "1",
    *tag_args,
    "origin",
    sha,
    check=False,
)
if result.returncode != 0:
    sys.stderr.write(result.stdout + result.stderr)
    raise SystemExit(result.returncode)
tree = git("rev-parse", f"{sha}^{{tree}}").stdout.strip()
new = git("commit-tree", tree, "-m", "bugrepro checkout").stdout.strip()
for ref in git("for-each-ref", "--format=%(refname)").stdout.splitlines():
    if ref and ref != "refs/heads/main":
        git("update-ref", "-d", ref)
git("update-ref", "refs/heads/main", new)
git("symbolic-ref", "HEAD", "refs/heads/main")
git("reset", "-q", "--hard")
for path in (".git/shallow", ".git/FETCH_HEAD", ".git/ORIG_HEAD", ".git/packed-refs"):
    try:
        os.remove(os.path.join(work, path))
    except FileNotFoundError:
        pass
git("reflog", "expire", "--expire=now", "--all")
git("gc", "-q", "--prune=now")
print(tree)
"""

_VERIFY_SCRIPT: Final = r"""
import json
import os
import subprocess

work = "/tmp/checkout"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", work, *args],
        text=True,
        capture_output=True,
        check=True,
    ).stdout


types = {}
objects = subprocess.run(
    [
        "git",
        "-C",
        work,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objecttype)",
    ],
    text=True,
    capture_output=True,
    check=True,
).stdout.split()
for kind in objects:
    types[kind] = types.get(kind, 0) + 1
refs = tuple(
    line for line in git("for-each-ref", "--format=%(refname)").splitlines() if line
)
print(
    json.dumps(
        {
            "commit_count": int(git("rev-list", "--all", "--count").strip()),
            "object_types": types,
            "synthetic_commit": git("rev-parse", "HEAD").strip(),
            "tree": git("rev-parse", "HEAD^{tree}").strip(),
            "shallow": os.path.exists(os.path.join(work, ".git/shallow")),
            "alternates": os.path.exists(
                os.path.join(work, ".git/objects/info/alternates")
            ),
            "refs": refs,
            "unreachable": bool(
                git("fsck", "--unreachable", "--no-reflogs").strip()
            ),
        }
    )
)
"""


def _validate_repo(
    repo_url: str,
    sha: str,
    *,
    test_repo: tuple[str, int] | None = None,
) -> None:
    try:
        parsed = urllib.parse.urlsplit(repo_url)
    except ValueError as exc:
        raise SetupFailed("fetch-failed", "invalid repository URL") from exc
    expected_host = test_repo[0] if test_repo is not None else "github.com"
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise SetupFailed(
            "fetch-failed", f"the repository URL must be https://{expected_host}"
        )
    if parsed.username or parsed.password:
        raise SetupFailed(
            "fetch-failed", "the repository URL must not contain credentials"
        )
    allowed_ports = {443}
    if test_repo is not None:
        allowed_ports.add(test_repo[1])
    if parsed.port is not None and parsed.port not in allowed_ports:
        raise SetupFailed("fetch-failed", "the repository URL must use port 443")
    if parsed.query or parsed.fragment:
        raise SetupFailed(
            "fetch-failed", "the repository URL must not have a query or fragment"
        )
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2 or any(segment in (".", "..") for segment in segments):
        raise SetupFailed("fetch-failed", "the repository URL must name one repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SetupFailed(
            "fetch-failed", "the commit must be a full 40-character hex SHA"
        )


def _proof_from_payload(payload: dict[str, object]) -> CheckoutProof:
    commit_count = payload.get("commit_count")
    object_types_raw = payload.get("object_types")
    commit = payload.get("synthetic_commit")
    tree = payload.get("tree")
    shallow = payload.get("shallow")
    alternates = payload.get("alternates")
    refs_raw = payload.get("refs")
    unreachable = payload.get("unreachable")
    if not isinstance(commit_count, int):
        raise TypeError("commit_count")
    if not isinstance(object_types_raw, dict):
        raise TypeError("object_types")
    object_types: dict[str, int] = {}
    for key, value in object_types_raw.items():
        if not isinstance(key, str) or not isinstance(value, int):
            raise TypeError("object_types")
        object_types[key] = value
    if not isinstance(commit, str) or not isinstance(tree, str):
        raise TypeError("commit or tree")
    if not isinstance(shallow, bool) or not isinstance(alternates, bool):
        raise TypeError("shallow or alternates")
    if not isinstance(refs_raw, list) or not all(
        isinstance(ref, str) for ref in refs_raw
    ):
        raise TypeError("refs")
    if not isinstance(unreachable, bool):
        raise TypeError("unreachable")
    return CheckoutProof(
        commit_count=commit_count,
        object_types=object_types,
        synthetic_commit=commit,
        tree=tree,
        shallow=shallow,
        alternates=alternates,
        refs=tuple(cast(list[str], refs_raw)),
        unreachable=unreachable,
    )


def _validate_checkout(proof: CheckoutProof, base_tree: str) -> None:
    if proof.commit_count != 1 or proof.object_types.get("commit") != 1:
        raise SetupFailed(
            "checkout-invalid", "the checkout does not have exactly one commit"
        )
    if proof.tree != base_tree:
        raise SetupFailed(
            "checkout-invalid",
            "the checkout tree does not match the requested commit",
        )
    if proof.shallow or proof.alternates or proof.unreachable:
        raise SetupFailed(
            "checkout-invalid",
            "the checkout kept shallow, alternate or unreachable objects",
        )
    if proof.refs != ("refs/heads/main",):
        raise SetupFailed("checkout-invalid", "the checkout has extra refs")


def verify_checkout(sandbox: Sandbox) -> CheckoutProof:
    """Ask the trusted fetcher to prove its checkout is one clean commit."""
    if sandbox.role != "fetch" or sandbox.image != FETCHER_IMAGE:
        raise ValueError("verify_checkout requires the trusted fetcher container")
    result = sandbox.run(["python", "-c", _VERIFY_SCRIPT], timeout=300)
    if not result.ok:
        raise SetupFailed(
            "checkout-invalid",
            "the checkout verification command failed",
            result.stdout + result.stderr,
        )
    payload = _last_json_object(result.stdout)
    if payload is None:
        raise SetupFailed(
            "checkout-invalid",
            "the checkout verification returned no JSON",
            result.stdout + result.stderr,
        )
    try:
        return _proof_from_payload(payload)
    except (TypeError, ValueError) as exc:
        raise SetupFailed(
            "checkout-invalid", f"the checkout verification is malformed: {exc}"
        ) from exc


@contextmanager
def fetch_checkout(
    repo_url: str,
    sha: str,
    *,
    run_id: str,
    deadline: Deadline,
    egress: EgressProxy,
    _git_verify_tls: bool = True,
    _test_repo: tuple[str, int] | None = None,
    _no_tags: bool = True,
) -> Iterator[Checkout]:
    """Fetch one commit in the trusted fetcher and yield a verified checkout.

    The proxy is switched to Install with an empty allowlist before yielding,
    so the project image cannot use the Fetch allowlist. Call
    ``checkout.copy_into(setup, destination)`` before leaving the block; the
    fetcher is removed when the block exits.
    """
    _validate_repo(repo_url, sha, test_repo=_test_repo)
    fetcher = Sandbox.start(
        FETCHER_IMAGE,
        run_id=run_id,
        deadline=deadline,
        egress=egress,
        role="fetch",
    )
    try:
        result = fetcher.run(
            [
                "python",
                "-c",
                _FETCH_SCRIPT,
                repo_url,
                sha,
                "1" if _git_verify_tls else "0",
                "1" if _no_tags else "0",
            ],
            timeout=600,
        )
        if not result.ok:
            proxy_logs = "\n".join(json.dumps(entry) for entry in egress.logs(tail=50))
            raise SetupFailed(
                "fetch-failed",
                "git fetch failed",
                result.stdout + result.stderr + proxy_logs,
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise SetupFailed("fetch-failed", "git fetch produced no tree")
        base_tree = lines[-1]
        proof = verify_checkout(fetcher)
        _validate_checkout(proof, base_tree)
        egress.allow(Phase.INSTALL, ())
        yield Checkout(
            fetcher=fetcher,
            synthetic_commit=proof.synthetic_commit,
            tree=proof.tree,
            base_sha=sha,
        )
    finally:
        fetcher.kill()
