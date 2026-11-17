from __future__ import annotations

import os
import shlex
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from bugrepro import build_parser, main
from bugrepro.sandbox import (
    DEFAULT_TIME_LIMIT_S,
    OUTPUT_LIMIT_BYTES,
    Deadline,
    DockerTooOld,
    DockerUnavailable,
    Sandbox,
    cleanup,
    create_network,
    ensure_supported_docker,
    new_run_id,
    run_isolated,
    run_scope,
)

TEST_IMAGE = (
    "python:3.12-alpine@sha256:"
    "c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
)

CONNECT = textwrap.dedent(
    """
    import time
    import urllib.request

    for attempt in range(50):
        try:
            with urllib.request.urlopen("http://server:8000/", timeout=2) as response:
                response.read(1)
            print("connected")
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise SystemExit("could not connect to the server")
    """
).strip()

CONNECT_ONCE = textwrap.dedent(
    """
    import urllib.request

    with urllib.request.urlopen("http://server:8000/", timeout=2) as response:
        response.read(1)
    print("connected")
    """
).strip()


@pytest.fixture(scope="session")
def test_image() -> str:
    _docker("pull", TEST_IMAGE)
    return TEST_IMAGE


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result


def _labelled(run_id: str) -> dict[str, list[str]]:
    label = f"--filter=label=bugrepro.run={run_id}"
    return {
        "containers": _docker("ps", "-aq", label).stdout.split(),
        "images": _docker("images", "-q", label).stdout.split(),
        "networks": _docker("network", "ls", "-q", label).stdout.split(),
    }


def _assert_empty(run_id: str) -> None:
    assert _labelled(run_id) == {
        "containers": [],
        "images": [],
        "networks": [],
    }


def _start_server(run_id: str, network: str) -> str:
    name = f"bugrepro-server-{run_id}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"bugrepro.run={run_id}",
        "--network",
        network,
        "--network-alias",
        "server",
        "--user",
        "1000:1000",
        TEST_IMAGE,
        "python",
        "-m",
        "http.server",
        "8000",
        "--bind",
        "0.0.0.0",
    )
    state = _docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip()
    assert state == "true", _docker("logs", name, check=False).stdout
    return name


def test_default_time_limit_is_1800() -> None:
    args = build_parser().parse_args(
        ["sandbox", "run", "--image", "alpine", "--", "true"]
    )
    assert DEFAULT_TIME_LIMIT_S == 1800
    assert args.time_limit == 1800


def test_time_limit_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "sandbox",
                "run",
                "--image",
                "alpine",
                "--time-limit",
                "0",
                "--",
                "true",
            ]
        )


def test_old_docker_is_rejected_before_any_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#17 adds the model; this proves the gate runs before any container."""
    log = tmp_path / "docker-calls.log"
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        'if [ "$1" = version ]; then\n'
        "  printf '27.5.1\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(DockerTooOld) as error:
        ensure_supported_docker()

    assert "27.5.1" in str(error.value)
    calls = log.read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("version")


def test_missing_docker_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DockerUnavailable):
        ensure_supported_docker()


@pytest.mark.docker
def test_outside_host_works_during_setup_and_fails_in_test(
    test_image: str,
) -> None:
    run_id = new_run_id()
    network = create_network(run_id)
    _start_server(run_id, network)
    sandbox: Sandbox | None = None
    try:
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=Deadline(120),
            network=network,
        )
        setup = sandbox.run(["python", "-c", CONNECT], timeout=60)
        assert setup.exit_code == 0, setup.stderr
        assert "connected" in setup.stdout

        frozen = sandbox.freeze()
        blocked = run_isolated(
            frozen,
            ["python", "-c", CONNECT_ONCE],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert blocked.exit_code != 0
        assert "connected" not in blocked.stdout
    finally:
        if sandbox is not None:
            sandbox.kill()
        failures = cleanup(run_id)

    assert failures == ()
    _assert_empty(run_id)


@pytest.mark.docker
def test_infinite_loop_stops_at_the_limit_and_cleans_up(test_image: str) -> None:
    run_id = new_run_id()
    started = time.monotonic()
    result = run_isolated(
        test_image,
        ["sh", "-c", "while :; do :; done"],
        timeout=10,
        run_id=run_id,
        deadline=Deadline(10),
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.exit_code == 124
    assert 9 <= elapsed < 25

    failures = cleanup(run_id)
    assert failures == ()
    _assert_empty(run_id)


@pytest.mark.docker
def test_cli_reports_stopped_at_time_limit(
    test_image: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "sandbox",
            "run",
            "--image",
            test_image,
            "--time-limit",
            "1",
            "--",
            "sh",
            "-c",
            "while :; do :; done",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 124
    assert "stopped-at-time-limit" in captured.out


@pytest.mark.docker
def test_freeze_keeps_files_but_not_processes(test_image: str) -> None:
    run_id = new_run_id()
    sandbox = Sandbox.start(
        test_image,
        run_id=run_id,
        deadline=Deadline(60),
    )
    try:
        setup = sandbox.run(
            "echo marker > /tmp/marker; sleep 300 >/dev/null 2>&1 & echo $! > /tmp/pid"
        )
        assert setup.exit_code == 0, setup.stderr
        frozen = sandbox.freeze()

        marker = run_isolated(
            frozen,
            ["cat", "/tmp/marker"],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert marker.exit_code == 0
        assert "marker" in marker.stdout

        alive = run_isolated(
            frozen,
            ["sh", "-c", "kill -0 $(cat /tmp/pid)"],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert alive.exit_code != 0
    finally:
        sandbox.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_copy_in_streams_a_directory(test_image: str, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello from the host\n")

    run_id = new_run_id()
    sandbox = Sandbox.start(
        test_image,
        run_id=run_id,
        deadline=Deadline(60),
    )
    try:
        sandbox.copy_in(source, "/tmp/input")
        result = sandbox.run(["cat", "/tmp/input/hello.txt"])
        assert result.exit_code == 0
        assert result.stdout == "hello from the host\n"
    finally:
        sandbox.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_output_is_capped(test_image: str) -> None:
    run_id = new_run_id()
    result = run_isolated(
        test_image,
        ["python", "-c", "print('x' * 2_000_000)"],
        timeout=60,
        run_id=run_id,
        deadline=Deadline(60),
    )

    assert result.exit_code == 0
    assert result.stdout_truncated
    assert len(result.stdout.encode()) <= OUTPUT_LIMIT_BYTES
    cleanup(run_id)


@pytest.mark.docker
def test_cleanup_runs_when_the_run_raises(test_image: str) -> None:
    run_id = new_run_id()
    with pytest.raises(RuntimeError), run_scope(run_id):
        Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=Deadline(60),
        )
        raise RuntimeError("boom")

    assert cleanup(run_id) == ()
    _assert_empty(run_id)
