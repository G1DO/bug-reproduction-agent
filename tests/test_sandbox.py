from __future__ import annotations

import ipaddress
import json
import os
import shlex
import socket
import ssl
import subprocess
import tarfile
import tempfile
import textwrap
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path

import pytest

from bugrepro import build_parser, main
from bugrepro.egress_proxy import Phase
from bugrepro.sandbox import (
    DEFAULT_TIME_LIMIT_S,
    FETCHER_IMAGE,
    OUTPUT_LIMIT_BYTES,
    PROXY_IMAGE,
    RUN_LABEL,
    CheckoutProof,
    CommandResult,
    Deadline,
    DockerTooOld,
    DockerUnavailable,
    EgressProxy,
    Sandbox,
    SandboxError,
    SetupFailed,
    _validate_checkout,
    cleanup,
    create_egress_network,
    create_network,
    ensure_supported_docker,
    fetch_checkout,
    new_run_id,
    proxy_env,
    run_isolated,
    run_scope,
    verify_checkout,
)

TEST_IMAGE = (
    "python:3.12-alpine@sha256:"
    "c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
)
FETCH_HOSTS = frozenset({"github.com"})

PROBE = textwrap.dedent(
    """
    import json
    import socket
    import sys
    import urllib.request

    gateway, gateway_port, private, private_port, fixture, fixture_port = sys.argv[1:7]
    results = {}


    def http(url):
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                response.read(1)
            return "connected"
        except Exception as error:
            return type(error).__name__


    def raw(host, port):
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return "connected"
        except Exception as error:
            return type(error).__name__


    results["metadata"] = http("https://169.254.169.254/")
    results["github"] = http("https://github.com/")
    results["gateway"] = http(f"https://{gateway}:{gateway_port}/")
    results["private"] = http(f"https://{private}:{private_port}/")
    results["ip_literal"] = http(f"https://{fixture}:{fixture_port}/")
    results["metadata_raw"] = raw("169.254.169.254", 80)
    results["gateway_raw"] = raw(gateway, gateway_port)
    results["private_raw"] = raw(private, private_port)
    try:
        socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
        results["dns"] = "resolved"
    except Exception as error:
        results["dns"] = type(error).__name__
    print(json.dumps(results))
    """
).strip()


@pytest.fixture(scope="session")
def test_image() -> str:
    _docker("pull", TEST_IMAGE)
    return TEST_IMAGE


@pytest.fixture(scope="session")
def fetcher_image() -> str:
    _docker("pull", FETCHER_IMAGE)
    return FETCHER_IMAGE


@pytest.fixture(scope="session")
def proxy_image() -> str:
    _docker("pull", PROXY_IMAGE)
    return PROXY_IMAGE


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


def _network_gateway(network: str) -> str:
    result = _docker(
        "network",
        "inspect",
        network,
        "--format",
        "{{(index .IPAM.Config 0).Gateway}}",
    )
    gateway = result.stdout.strip()
    assert gateway, f"{network} has no gateway"
    return gateway


def _default_bridge_gateway() -> str:
    return _network_gateway("bridge")


def _host_ip() -> str:
    address = socket.gethostbyname(socket.gethostname())
    if address.startswith("127."):
        return _default_bridge_gateway()
    return address


def _private_ip() -> str:
    gateway = _default_bridge_gateway()
    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        candidates = []
    for candidate in candidates:
        if not candidate.startswith("127.") and candidate != gateway:
            return candidate
    return "192.168.1.1"


class _EchoServer:
    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(("0.0.0.0", 0))
        self.socket.listen(1)
        self.port = int(self.socket.getsockname()[1])
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        with connection:
            while data := connection.recv(4096):
                connection.sendall(data)

    def close(self) -> None:
        self.socket.close()


class _HostListener:
    def __init__(self) -> None:
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


def _start_egress(
    run_id: str,
    *,
    deadline: Deadline,
    phase: Phase,
    hosts: Sequence[str],
    test_routes: Mapping[tuple[str, int], str] | None = None,
) -> tuple[EgressProxy, str, str]:
    internal = create_network(run_id)
    egress = create_egress_network(run_id)
    proxy = EgressProxy.start(
        run_id=run_id,
        deadline=deadline,
        internal_network=internal,
        egress_network=egress,
        phase=phase,
        hosts=hosts,
        _test_routes=test_routes,
    )
    return proxy, internal, egress


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
        Sandbox.start(TEST_IMAGE, run_id="old-docker", deadline=Deadline(10))

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
def test_test_phase_has_no_network(test_image: str) -> None:
    listener = _HostListener()
    try:
        result = run_isolated(
            test_image,
            [
                "python",
                "-c",
                (
                    "import socket, sys\n"
                    "try:\n"
                    "    socket.create_connection((sys.argv[1], int(sys.argv[2])),"
                    " timeout=2)\n"
                    "    print('connected')\n"
                    "except OSError as error:\n"
                    "    print(type(error).__name__)\n"
                ),
                _host_ip(),
                str(listener.port),
            ],
            timeout=30,
            run_id=new_run_id(),
            deadline=Deadline(30),
        )
    finally:
        listener.close()
    assert result.ok
    assert "connected" not in result.stdout
    assert "OSError" in result.stdout


@pytest.mark.docker
def test_setup_probes_are_refused(test_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(180)
    listener = _HostListener()
    gateway = _default_bridge_gateway()
    private = _private_ip()
    assert ipaddress.ip_address(private).is_private
    assert private != gateway
    sandbox: Sandbox | None = None
    proxy: EgressProxy | None = None
    try:
        internal = create_network(run_id)
        egress_network = create_egress_network(run_id)
        fixture = _network_gateway(egress_network)
        proxy = EgressProxy.start(
            run_id=run_id,
            deadline=deadline,
            internal_network=internal,
            egress_network=egress_network,
            phase=Phase.INSTALL,
            hosts=["pypi.org", "fixture-git"],
            _test_routes={("fixture-git", listener.port): fixture},
        )
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="setup",
        )
        started = datetime.now(UTC)
        result = sandbox.run(
            [
                "python",
                "-c",
                PROBE,
                gateway,
                str(listener.port),
                private,
                str(listener.port),
                fixture,
                str(listener.port),
            ],
            timeout=60,
        )
        finished = datetime.now(UTC)
        assert result.ok, result.stderr
        outcomes = json.loads(result.stdout)
        for name, outcome in outcomes.items():
            assert outcome not in ("connected", "resolved"), name

        entries = [
            entry
            for entry in proxy.logs()
            if entry.get("event") == "request" and entry.get("decision") == "deny"
        ]
        assert len(entries) == 5
        hosts = {str(entry.get("host")) for entry in entries}
        assert hosts == {
            "169.254.169.254",
            "github.com",
            gateway,
            private,
            fixture,
        }
        reasons = {str(entry.get("reason")) for entry in entries}
        assert reasons == {"ip-literal", "not-allowlisted"}
        assert all(entry.get("phase") == "install" for entry in entries)
        for entry in entries:
            moment = datetime.fromisoformat(str(entry["time"]))
            assert started - timedelta(seconds=2) <= moment
            assert moment <= finished + timedelta(seconds=2)
    finally:
        listener.close()
        if sandbox is not None:
            sandbox.kill()
        if proxy is not None:
            proxy.kill()
        failures = cleanup(run_id)

    assert failures == ()
    _assert_empty(run_id)


DENIED = textwrap.dedent(
    """
    import socket
    import sys

    port = int(sys.argv[1])
    client = socket.create_connection(("egress", 3128), timeout=5)
    client.sendall(f"CONNECT fixture-echo:{port} HTTP/1.1\\r\\n\\r\\n".encode())
    response = client.recv(4096)
    print("allowed" if b"200" in response else "denied")
    """
).strip()


TUNNEL = textwrap.dedent(
    """
    import socket
    import sys

    port = int(sys.argv[1])
    client = socket.create_connection(("egress", 3128), timeout=5)
    client.sendall(f"CONNECT fixture-echo:{port} HTTP/1.1\\r\\n\\r\\n".encode())
    head = b""
    while b"\\r\\n\\r\\n" not in head:
        chunk = client.recv(4096)
        if not chunk:
            raise SystemExit("no response")
        head += chunk
    if b"200" not in head:
        raise SystemExit(head.decode("utf-8", "replace"))
    open("/tmp/open", "w").close()
    client.settimeout(30)
    try:
        data = client.recv(1)
        print("closed" if not data else "data")
    except Exception as error:
        print(type(error).__name__)
    """
).strip()


@pytest.mark.docker
def test_allow_closes_open_tunnels(test_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(120)
    echo = _EchoServer()
    sandbox: Sandbox | None = None
    proxy: EgressProxy | None = None
    try:
        internal = create_network(run_id)
        egress = create_egress_network(run_id)
        proxy = EgressProxy.start(
            run_id=run_id,
            deadline=deadline,
            internal_network=internal,
            egress_network=egress,
            phase=Phase.FETCH,
            hosts=sorted(FETCH_HOSTS),
            _test_routes={("fixture-echo", echo.port): _network_gateway(egress)},
        )
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="probe",
        )
        holder: dict[str, CommandResult] = {}

        def run_tunnel() -> None:
            holder["result"] = sandbox.run(
                ["python", "-c", TUNNEL, str(echo.port)], timeout=60
            )

        thread = threading.Thread(target=run_tunnel)
        thread.start()
        opened = False
        for _ in range(100):
            if sandbox.run(["test", "-f", "/tmp/open"], timeout=5).ok:
                opened = True
                break
            time.sleep(0.05)
        assert opened
        assert sandbox is not None
        sandbox.allow(Phase.INSTALL, ["pypi.org"])
        thread.join(timeout=15)
        assert not thread.is_alive()
        result = holder["result"]
        assert "closed" in result.stdout
        denied = sandbox.run(["python", "-c", DENIED, str(echo.port)], timeout=15)
        assert denied.ok
        assert "denied" in denied.stdout
        assert any(
            entry.get("event") == "allowlist" and entry.get("phase") == "install"
            for entry in proxy.logs()
        )
    finally:
        echo.close()
        if sandbox is not None:
            sandbox.kill()
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_proxy_death_is_visible(test_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(120)
    sandbox: Sandbox | None = None
    proxy: EgressProxy | None = None
    try:
        proxy, _, _ = _start_egress(
            run_id,
            deadline=deadline,
            phase=Phase.INSTALL,
            hosts=["pypi.org"],
        )
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="setup",
        )
        proxy.kill()
        result = sandbox.run(
            [
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('https://pypi.org')",
            ],
            timeout=15,
        )
        assert not result.ok
        assert not proxy.healthy()
        with pytest.raises(SetupFailed):
            proxy.allow(Phase.INSTALL, ["pypi.org"])
    finally:
        if sandbox is not None:
            sandbox.kill()
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_frozen_image_has_no_proxy_env(test_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(120)
    proxy: EgressProxy | None = None
    try:
        proxy, _, _ = _start_egress(
            run_id,
            deadline=deadline,
            phase=Phase.INSTALL,
            hosts=["pypi.org"],
        )
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="setup",
        )
        inside = sandbox.run(["sh", "-c", "printf '%s' \"$HTTP_PROXY\""])
        assert "egress:3128" in inside.stdout
        frozen = sandbox.freeze()

        inspect = _docker(
            "image", "inspect", frozen.ref, "--format", "{{json .Config.Env}}"
        )
        environment = json.loads(inspect.stdout)
        for entry in environment:
            key, _, value = entry.partition("=")
            if key in {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "http_proxy",
                "https_proxy",
                "NO_PROXY",
                "no_proxy",
            }:
                assert value == "", (key, value)

        outside = run_isolated(
            frozen,
            ["sh", "-c", "printf '%s' \"$HTTP_PROXY\""],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert outside.stdout == ""
    finally:
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)


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

    _assert_empty(run_id)
    assert cleanup(run_id) == ()


@dataclass(frozen=True)
class _Fixture:
    port: int
    base_sha: str
    base_tree: str
    fix_sha: str


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_fixture_repo(root: Path) -> tuple[str, str, str]:
    work = root / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "fixture@example.invalid")
    _git(work, "config", "user.name", "fixture")
    (work / "base.txt").write_text("base\n")
    (work / "link").symlink_to("/etc/passwd")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    base = _git(work, "rev-parse", "HEAD").stdout.strip()
    base_tree = _git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
    _git(work, "tag", "v1", base)
    (work / "fix.txt").write_text("fix\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "fix")
    fix = _git(work, "rev-parse", "HEAD").stdout.strip()
    owner = root / "owner"
    owner.mkdir()
    bare = owner / "repo.git"
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "config", "uploadpack.allowReachableSHA1InWant", "true")
    return base, base_tree, fix


def _make_certificate(root: Path) -> tuple[Path, Path]:
    certificate = root / "cert.pem"
    key = root / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=fixture-git",
        ],
        capture_output=True,
        check=True,
    )
    return certificate, key


@contextmanager
def _git_server() -> Iterator[_Fixture]:
    with tempfile.TemporaryDirectory(prefix="bugrepro-git-") as tmp:
        root = Path(tmp)
        base, base_tree, fix = _make_fixture_repo(root)
        certificate, key = _make_certificate(root)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                self._run()

            def do_POST(self) -> None:
                self._run()

            def _run(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlsplit(self.path)
                environment = os.environ.copy()
                environment.update(
                    {
                        "GIT_PROJECT_ROOT": str(root),
                        "GIT_HTTP_EXPORT_ALL": "1",
                        "PATH_INFO": parsed.path,
                        "QUERY_STRING": parsed.query,
                        "REQUEST_METHOD": self.command,
                        "CONTENT_TYPE": self.headers.get_content_type(),
                        "CONTENT_LENGTH": str(length),
                        "REMOTE_ADDR": self.client_address[0],
                        "SERVER_PROTOCOL": self.protocol_version,
                        "SERVER_NAME": "fixture-git",
                        "SERVER_PORT": str(getattr(self.server, "server_port", 0)),
                        "GATEWAY_INTERFACE": "CGI/1.1",
                        "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                        "HOME": str(root),
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull,
                    }
                )
                process = subprocess.run(
                    ["git", "http-backend"],
                    input=body,
                    env=environment,
                    capture_output=True,
                    check=False,
                )
                if process.returncode != 0:
                    self.send_error(500)
                    return
                head, separator, payload = process.stdout.partition(b"\r\n\r\n")
                if not separator:
                    head, separator, payload = process.stdout.partition(b"\n\n")
                status = 200
                headers: list[tuple[str, str]] = []
                for line in head.splitlines():
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"status":
                        status = int(value.strip().split()[0])
                    elif name.strip():
                        headers.append((name.strip().decode(), value.strip().decode()))
                self.send_response(status)
                self.send_header("Connection", "close")
                for header_name, header_value in headers:
                    self.send_header(header_name, header_value)
                if not any(name.lower() == "content-length" for name, _ in headers):
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

        server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        port = int(server.server_address[1])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield _Fixture(port=port, base_sha=base, base_tree=base_tree, fix_sha=fix)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


@pytest.mark.docker
def test_checkout_is_one_commit_with_base_tree(
    fetcher_image: str, proxy_image: str
) -> None:
    run_id = new_run_id()
    deadline = Deadline(300)
    with _git_server() as fixture:
        proxy: EgressProxy | None = None
        setup: Sandbox | None = None
        try:
            egress_net = create_egress_network(run_id)
            internal = create_network(run_id)
            gateway = _network_gateway(egress_net)
            proxy = EgressProxy.start(
                run_id=run_id,
                deadline=deadline,
                internal_network=internal,
                egress_network=egress_net,
                phase=Phase.FETCH,
                hosts=FETCH_HOSTS,
                _test_routes={("fixture-git", fixture.port): gateway},
            )
            url = f"https://fixture-git:{fixture.port}/owner/repo.git"
            with fetch_checkout(
                url,
                fixture.base_sha,
                run_id=run_id,
                deadline=deadline,
                egress=proxy,
                _git_verify_tls=False,
                _test_repo=("fixture-git", fixture.port),
            ) as checkout:
                assert checkout.tree == fixture.base_tree
                assert checkout.synthetic_commit != checkout.base_sha
                assert proxy.phase is Phase.INSTALL
                setup = Sandbox.start(
                    fetcher_image,
                    run_id=run_id,
                    deadline=deadline,
                    egress=None,
                    role="setup",
                )
                checkout.copy_into(setup, "/tmp/checkout")
                count = setup.run(
                    ["git", "-C", "/tmp/checkout", "rev-list", "--all", "--count"]
                )
                assert count.stdout.strip() == "1"
                tree = setup.run(
                    ["git", "-C", "/tmp/checkout", "rev-parse", "HEAD^{tree}"]
                )
                assert tree.stdout.strip() == fixture.base_tree
                fix = setup.run(
                    [
                        "git",
                        "-C",
                        "/tmp/checkout",
                        "cat-file",
                        "-e",
                        fixture.fix_sha,
                    ]
                )
                assert not fix.ok
                unreachable = setup.run(
                    [
                        "git",
                        "-C",
                        "/tmp/checkout",
                        "fsck",
                        "--unreachable",
                        "--no-reflogs",
                    ]
                )
                assert unreachable.stdout.strip() == ""
                refs = setup.run(
                    [
                        "git",
                        "-C",
                        "/tmp/checkout",
                        "for-each-ref",
                        "--format=%(refname)",
                    ]
                )
                assert refs.stdout.strip() == "refs/heads/main"
                link = setup.run(["readlink", "/tmp/checkout/link"])
                assert link.stdout.strip() == "/etc/passwd"
        finally:
            if setup is not None:
                setup.kill()
            if proxy is not None:
                proxy.kill()
            cleanup(run_id)

    assert cleanup(run_id) == ()
    _assert_empty(run_id)


@pytest.mark.docker
def test_fetch_failure_is_setup_failed(fetcher_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(180)
    with _git_server() as fixture:
        proxy: EgressProxy | None = None
        try:
            egress_net = create_egress_network(run_id)
            internal = create_network(run_id)
            gateway = _network_gateway(egress_net)
            proxy = EgressProxy.start(
                run_id=run_id,
                deadline=deadline,
                internal_network=internal,
                egress_network=egress_net,
                phase=Phase.FETCH,
                hosts=FETCH_HOSTS,
                _test_routes={("fixture-git", fixture.port): gateway},
            )
            url = f"https://fixture-git:{fixture.port}/owner/repo.git"
            with (
                pytest.raises(SetupFailed) as error,
                fetch_checkout(
                    url,
                    "0" * 40,
                    run_id=run_id,
                    deadline=deadline,
                    egress=proxy,
                    _git_verify_tls=False,
                    _test_repo=("fixture-git", fixture.port),
                ),
            ):
                pass
            assert error.value.reason == "fetch-failed"
            assert error.value.output
        finally:
            if proxy is not None:
                proxy.kill()
            cleanup(run_id)


@pytest.mark.docker
def test_harness_route_cannot_be_enabled_publicly(
    fetcher_image: str, proxy_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as exit_error:
        build_parser().parse_args(
            [
                "sandbox",
                "run",
                "--image",
                "alpine",
                "--allow-private-egress",
                "--",
                "true",
            ]
        )
    assert exit_error.value.code == 2

    monkeypatch.setenv("BUGREPRO_ALLOW_PRIVATE_EGRESS", "1")
    run_id = new_run_id()
    deadline = Deadline(180)
    with _git_server() as fixture:
        proxy: EgressProxy | None = None
        try:
            proxy, _, _ = _start_egress(
                run_id,
                deadline=deadline,
                phase=Phase.FETCH,
                hosts=sorted(FETCH_HOSTS),
            )
            url = f"https://fixture-git:{fixture.port}/owner/repo.git"
            with (
                pytest.raises(SetupFailed) as error,
                fetch_checkout(
                    url,
                    fixture.base_sha,
                    run_id=run_id,
                    deadline=deadline,
                    egress=proxy,
                    _git_verify_tls=False,
                    _test_repo=("fixture-git", fixture.port),
                ),
            ):
                pass
            assert error.value.reason == "fetch-failed"
            assert "403" in error.value.output or "refused" in error.value.output
            entries = [
                entry for entry in proxy.logs() if entry.get("event") == "request"
            ]
            assert any(
                entry.get("host") == "fixture-git" and entry.get("decision") == "deny"
                for entry in entries
            )
            assert not any(entry.get("reason") == "test-override" for entry in entries)
        finally:
            if proxy is not None:
                proxy.kill()
            cleanup(run_id)


def _unit_sandbox(*, role: str = "setup", image: str = TEST_IMAGE) -> Sandbox:
    return Sandbox(
        container="unit",
        run_id="unit",
        deadline=Deadline(10),
        image=image,
        role=role,
    )


def _proof(**changes: object) -> CheckoutProof:
    values: dict[str, object] = {
        "commit_count": 1,
        "object_types": {"commit": 1, "tree": 1, "blob": 1},
        "synthetic_commit": "a" * 40,
        "tree": "tree",
        "shallow": False,
        "alternates": False,
        "refs": ("refs/heads/main",),
        "unreachable": False,
    }
    values.update(changes)
    return CheckoutProof(**values)  # type: ignore[arg-type]


def test_setup_cannot_start_in_fetch_phase() -> None:
    proxy = EgressProxy(
        container="unit",
        run_id="unit",
        deadline=Deadline(10),
        internal_network="internal",
        egress_network="egress",
        phase=Phase.FETCH,
    )
    with pytest.raises(SetupFailed) as error:
        Sandbox.start(
            TEST_IMAGE,
            run_id="unit",
            deadline=Deadline(10),
            egress=proxy,
            role="setup",
        )
    assert error.value.reason == "phase-order"


def test_fetch_role_requires_fetcher_image() -> None:
    proxy = EgressProxy(
        container="unit",
        run_id="unit",
        deadline=Deadline(10),
        internal_network="internal",
        egress_network="egress",
        phase=Phase.FETCH,
    )
    with pytest.raises(SetupFailed) as error:
        Sandbox.start(
            TEST_IMAGE,
            run_id="unit",
            deadline=Deadline(10),
            egress=proxy,
            role="fetch",
        )
    assert error.value.reason == "phase-order"


def test_sandbox_allow_requires_egress() -> None:
    with pytest.raises(SandboxError):
        _unit_sandbox().allow(Phase.INSTALL, [])


def test_verify_checkout_requires_fetcher() -> None:
    with pytest.raises(ValueError):
        verify_checkout(_unit_sandbox())


def test_copy_in_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("host data\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SandboxError):
        _unit_sandbox().copy_in(link, "/tmp/input")


def test_copy_from_requires_fetcher() -> None:
    with pytest.raises(SandboxError):
        _unit_sandbox().copy_from(
            _unit_sandbox(role="setup"), "/tmp/checkout", "/tmp/checkout"
        )


def test_invalid_run_ids_are_refused() -> None:
    for run_id in ("bad,id", "bad\nid", "", "a" * 65):
        with pytest.raises(ValueError):
            cleanup(run_id)
        with pytest.raises(ValueError):
            Sandbox.start(TEST_IMAGE, run_id=run_id, deadline=Deadline(10))


def test_proxy_env_sets_all_proxy() -> None:
    environment = proxy_env(["mirror"])
    assert environment["ALL_PROXY"] == environment["HTTPS_PROXY"]
    assert environment["all_proxy"] == environment["HTTPS_PROXY"]
    assert environment["NO_PROXY"] == "mirror"


def test_validate_checkout_accepts_clean_proof() -> None:
    _validate_checkout(_proof(), "tree")


@pytest.mark.parametrize(
    "changes",
    [
        {"commit_count": 2},
        {"object_types": {"commit": 2, "tree": 1}},
        {"tree": "other"},
        {"shallow": True},
        {"alternates": True},
        {"unreachable": True},
        {"refs": ("refs/heads/main", "refs/tags/v1")},
    ],
)
def test_validate_checkout_rejects_bad_proof(changes: dict[str, object]) -> None:
    with pytest.raises(SetupFailed) as error:
        _validate_checkout(_proof(**changes), "tree")
    assert error.value.reason == "checkout-invalid"


def _build_image(tag: str, dockerfile: str) -> None:
    result = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        input=dockerfile,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.docker
def test_entrypoint_is_not_run(test_image: str) -> None:
    run_id = new_run_id()
    image = f"bugrepro-entrytest-{run_id.lower()}:latest"
    dockerfile = (
        f"FROM {test_image}\n"
        'ENTRYPOINT ["sh", "-c", "echo ran > /tmp/entrypoint; exec \\"$@\\"", "sh"]\n'
    )
    _build_image(image, dockerfile)
    sandbox: Sandbox | None = None
    try:
        sandbox = Sandbox.start(image, run_id=run_id, deadline=Deadline(60))
        check = sandbox.run(["sh", "-c", "test ! -e /tmp/entrypoint"])
        assert check.ok, (check.stdout, check.stderr)
        frozen = sandbox.freeze()
        result = run_isolated(
            frozen,
            ["sh", "-c", "test ! -e /tmp/entrypoint"],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert result.ok, (result.stdout, result.stderr)
    finally:
        if sandbox is not None:
            sandbox.kill()
        _docker("rmi", "-f", image, check=False)
        cleanup(run_id)


@pytest.mark.docker
def test_copy_in_tar_error_fails_fast(
    test_image: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("content\n")
    run_id = new_run_id()
    sandbox = Sandbox.start(test_image, run_id=run_id, deadline=Deadline(60))

    def broken(*args: object, **kwargs: object) -> None:
        raise tarfile.TarError("boom")

    monkeypatch.setattr(tarfile.TarFile, "add", broken)
    started = time.monotonic()
    try:
        with pytest.raises(SandboxError) as error:
            sandbox.copy_in(source, "/tmp/input")
        assert "boom" in str(error.value)
        assert time.monotonic() - started < 3
        assert sandbox.run(["true"]).ok
    finally:
        sandbox.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_per_command_timeout_keeps_the_sandbox(test_image: str) -> None:
    run_id = new_run_id()
    sandbox = Sandbox.start(test_image, run_id=run_id, deadline=Deadline(60))
    try:
        result = sandbox.run(["sleep", "10"], timeout=0.2)
        assert result.timed_out
        assert result.exit_code == 124
        assert sandbox.run(["true"]).ok
    finally:
        sandbox.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_freeze_scrubs_prebuilt_proxy_env(test_image: str) -> None:
    run_id = new_run_id()
    image = f"bugrepro-envtest-{run_id.lower()}:latest"
    source = _docker("create", test_image, "true").stdout.strip()
    _docker(
        "commit",
        "--change",
        "ENV HTTP_PROXY=http://dead:1",
        "--change",
        "ENV ALL_PROXY=http://dead:1",
        "--change",
        f"LABEL {RUN_LABEL}={run_id}",
        source,
        image,
    )
    _docker("rm", "-f", source)
    sandbox: Sandbox | None = None
    try:
        sandbox = Sandbox.start(image, run_id=run_id, deadline=Deadline(60))
        inside = sandbox.run(["sh", "-c", "printf '%s' \"$HTTP_PROXY\""])
        assert inside.stdout == ""
        frozen = sandbox.freeze()
        inspect = _docker(
            "image", "inspect", frozen.ref, "--format", "{{json .Config.Env}}"
        )
        for entry in json.loads(inspect.stdout):
            key, _, value = entry.partition("=")
            if key in {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "http_proxy",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
                "NO_PROXY",
                "no_proxy",
            }:
                assert value == "", (key, value)
        outside = run_isolated(
            frozen,
            ["sh", "-c", "printf '%s' \"$HTTP_PROXY\""],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
        )
        assert outside.stdout == ""
    finally:
        if sandbox is not None:
            sandbox.kill()
        _docker("rmi", "-f", image, check=False)
        cleanup(run_id)


@pytest.mark.docker
def test_freeze_refs_are_unique(test_image: str) -> None:
    run_id = new_run_id()
    first = Sandbox.start(test_image, run_id=run_id, deadline=Deadline(60))
    second = Sandbox.start(test_image, run_id=run_id, deadline=Deadline(60))
    try:
        first_image = first.freeze()
        second_image = second.freeze()
        assert first_image.ref != second_image.ref
        for image in (first_image, second_image):
            result = run_isolated(
                image,
                ["true"],
                timeout=30,
                run_id=run_id,
                deadline=Deadline(30),
            )
            assert result.ok
    finally:
        first.kill()
        second.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_fetch_drops_fetched_tags(fetcher_image: str, proxy_image: str) -> None:
    run_id = new_run_id()
    deadline = Deadline(240)
    with _git_server() as fixture:
        proxy: EgressProxy | None = None
        try:
            egress_net = create_egress_network(run_id)
            internal = create_network(run_id)
            gateway = _network_gateway(egress_net)
            proxy = EgressProxy.start(
                run_id=run_id,
                deadline=deadline,
                internal_network=internal,
                egress_network=egress_net,
                phase=Phase.FETCH,
                hosts=FETCH_HOSTS,
                _test_routes={("fixture-git", fixture.port): gateway},
            )
            url = f"https://fixture-git:{fixture.port}/owner/repo.git"
            with fetch_checkout(
                url,
                fixture.base_sha,
                run_id=run_id,
                deadline=deadline,
                egress=proxy,
                _git_verify_tls=False,
                _test_repo=("fixture-git", fixture.port),
                _no_tags=False,
            ) as checkout:
                assert checkout.tree == fixture.base_tree
        finally:
            if proxy is not None:
                proxy.kill()
            cleanup(run_id)

    assert cleanup(run_id) == ()
    _assert_empty(run_id)


@pytest.mark.docker
def test_control_socket_is_not_reachable_from_setup(
    test_image: str, proxy_image: str
) -> None:
    run_id = new_run_id()
    deadline = Deadline(120)
    sandbox: Sandbox | None = None
    proxy: EgressProxy | None = None
    try:
        proxy, _, _ = _start_egress(
            run_id,
            deadline=deadline,
            phase=Phase.INSTALL,
            hosts=["pypi.org"],
        )
        sandbox = Sandbox.start(
            test_image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="setup",
        )
        script = (
            "import socket\n"
            "try:\n"
            "    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    client.connect('/tmp/bugrepro-egress.sock')\n"
            "    print('connected')\n"
            "except OSError as error:\n"
            "    print(type(error).__name__)\n"
        )
        result = sandbox.run(["python", "-c", script], timeout=15)
        assert result.ok
        assert "connected" not in result.stdout
    finally:
        if sandbox is not None:
            sandbox.kill()
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)


@pytest.mark.docker
def test_image_ref_cannot_inject_docker_options(test_image: str) -> None:
    run_id = new_run_id()
    try:
        with pytest.raises(SandboxError):
            Sandbox.start("--volume=/:/host", run_id=run_id, deadline=Deadline(10))
        result = run_isolated(
            "--volume=/:/host",
            ["sh", "-c", "cat /host/etc/passwd"],
            timeout=10,
            run_id=run_id,
            deadline=Deadline(10),
        )
        assert not result.ok
        assert "/host" not in result.stdout
    finally:
        cleanup(run_id)


@pytest.mark.docker
def test_tool_env_is_passed_and_reserved_keys_are_refused(test_image: str) -> None:
    run_id = new_run_id()
    sandbox = Sandbox.start(test_image, run_id=run_id, deadline=Deadline(60))
    try:
        inside = sandbox.run(
            ["sh", "-c", "printf '%s' \"$BUGREPRO_TEST_VALUE\""],
            env={"BUGREPRO_TEST_VALUE": "inside"},
        )
        assert inside.ok
        assert inside.stdout == "inside"
        with pytest.raises(ValueError):
            sandbox.run(["true"], env={"HOME": "/root"})
    finally:
        sandbox.kill()
        cleanup(run_id)

    outside = run_isolated(
        test_image,
        ["sh", "-c", "printf '%s' \"$BUGREPRO_TEST_VALUE\""],
        timeout=30,
        run_id=run_id,
        deadline=Deadline(30),
        env={"BUGREPRO_TEST_VALUE": "outside"},
    )
    assert outside.ok
    assert outside.stdout == "outside"
    with pytest.raises(ValueError):
        run_isolated(
            test_image,
            ["true"],
            timeout=30,
            run_id=run_id,
            deadline=Deadline(30),
            env={"HTTPS_PROXY": "http://evil"},
        )
    cleanup(run_id)
