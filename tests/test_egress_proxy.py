from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from bugrepro import egress_proxy
from bugrepro.egress_proxy import (
    FETCH_HOSTS,
    Decision,
    Phase,
    ProxyState,
    Tunnel,
    _apply_allowlist,
    _handle_client,
    _handle_control,
    _log,
    _send_control,
    _serve_control,
    decide,
    is_ip_literal,
    is_public_unicast,
    normalize_host,
    parse_connect,
    serve,
    validate_policy,
    validate_transition,
)
from bugrepro.sandbox import Deadline, EgressProxy, SetupFailed


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _control_path() -> str:
    return f"/tmp/bugrepro-test-{uuid.uuid4().hex}.sock"


def _wait_for_control(path: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _send_control(path, {"action": "ping"})
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError("the proxy control socket did not appear")


class _EchoServer:
    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
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


def test_parse_connect_accepts_hostname_and_ipv6() -> None:
    assert parse_connect("CONNECT example.com:443 HTTP/1.1") == ("example.com", 443)
    assert parse_connect("CONNECT [2001:db8::1]:443 HTTP/1.0") == (
        "2001:db8::1",
        443,
    )


@pytest.mark.parametrize(
    "line",
    [
        "GET example.com:443 HTTP/1.1",
        "CONNECT example.com HTTP/1.1",
        "CONNECT example.com:0 HTTP/1.1",
        "CONNECT example.com:443 HTTP/2",
        "CONNECT a:b:443 HTTP/1.1",
        "CONNECT  example.com:443 HTTP/1.1",
        "CONNECT [::1:443 HTTP/1.1",
        "CONNECT example.com:443",
    ],
)
def test_parse_connect_rejects_bad_requests(line: str) -> None:
    with pytest.raises(ValueError):
        parse_connect(line)


def test_normalize_host() -> None:
    assert normalize_host("Example.COM.") == "example.com"
    for bad in ("", "a..b", "-a.com", "a-.com", "a_b.com", "a" * 64 + ".com"):
        with pytest.raises(ValueError):
            normalize_host(bad)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("169.254.169.254", True),
        ("::1", True),
        ("[::1]", True),
        ("example.com", False),
        ("1.2.3.4.example.com", False),
    ],
)
def test_is_ip_literal(host: str, expected: bool) -> None:
    assert is_ip_literal(host) is expected


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2606:4700:4700::1111",
        "::ffff:8.8.8.8",
    ],
)
def test_public_unicast_addresses(address: str) -> None:
    assert is_public_unicast(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "239.255.255.250",
        "::",
        "::1",
        "ff02::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        "100.64.0.1",
        "198.18.0.1",
        "192.0.2.1",
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert is_public_unicast(address) is False


def test_decide_allows_public_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_proxy, "resolve_once", lambda host, port: ("8.8.8.8",))
    decision = decide("pypi.org", 443, frozenset({"pypi.org"}))
    assert decision == Decision(True, "allowlisted", "pypi.org", 443, ("8.8.8.8",))


@pytest.mark.parametrize(
    "address",
    ["10.0.0.1", "224.0.0.1", "::ffff:169.254.169.254"],
)
def test_decide_denies_non_public(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(egress_proxy, "resolve_once", lambda host, port: (address,))
    decision = decide("pypi.org", 443, frozenset({"pypi.org"}))
    assert decision.allowed is False
    assert decision.reason == "non-global-address"


@pytest.mark.parametrize("host", ["169.254.169.254", "::1", "[::1]"])
def test_decide_denies_ip_literal_before_dns(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    calls = 0

    def fail(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        raise AssertionError("DNS must not run")

    monkeypatch.setattr(egress_proxy, "resolve_once", fail)
    decision = decide(host, 443, FETCH_HOSTS)
    assert decision.allowed is False
    assert decision.reason == "ip-literal"
    assert calls == 0


def test_decide_denies_not_allowlisted_before_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        raise AssertionError("DNS must not run")

    monkeypatch.setattr(egress_proxy, "resolve_once", fail)
    decision = decide("github.com", 443, frozenset({"pypi.org"}))
    assert decision.allowed is False
    assert decision.reason == "not-allowlisted"
    assert calls == 0


def test_decide_denies_other_ports() -> None:
    decision = decide("pypi.org", 80, frozenset({"pypi.org"}))
    assert decision.allowed is False
    assert decision.reason == "port-not-allowed"


def test_decide_resolves_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def resolve(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ("8.8.8.8",)

    monkeypatch.setattr(egress_proxy, "resolve_once", resolve)
    decision = decide("pypi.org", 443, frozenset({"pypi.org"}))
    assert decision.allowed is True
    assert calls == 1


def test_test_route_bypasses_checks() -> None:
    decision = decide(
        "fixture-git",
        1234,
        frozenset(),
        {("fixture-git", 1234): "172.17.0.1"},
    )
    assert decision.allowed is True
    assert decision.reason == "test-override"
    assert decision.addresses == ("172.17.0.1",)


def test_validate_policy() -> None:
    assert validate_policy(Phase.FETCH, ["github.com"]) == FETCH_HOSTS
    assert validate_policy(Phase.INSTALL, ["pypi.org"]) == frozenset({"pypi.org"})
    with pytest.raises(ValueError):
        validate_policy(Phase.FETCH, ["pypi.org"])
    with pytest.raises(ValueError):
        validate_policy(Phase.INSTALL, ["github.com"])
    with pytest.raises(ValueError):
        validate_policy(Phase.TEST, [])
    for github in (
        "github.com",
        "api.github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "github.io",
    ):
        with pytest.raises(ValueError):
            validate_policy(Phase.INSTALL, [github])
    assert validate_policy(Phase.INSTALL, ["github.com.evil"]) == frozenset(
        {"github.com.evil"}
    )


def test_validate_transition() -> None:
    validate_transition(Phase.FETCH, Phase.INSTALL)
    validate_transition(Phase.INSTALL, Phase.INSTALL)
    with pytest.raises(ValueError):
        validate_transition(Phase.INSTALL, Phase.FETCH)


def test_apply_allowlist_closes_tunnels() -> None:
    client, upstream = socket.socketpair()
    tunnel = Tunnel(client=client, upstream=upstream, generation=0)
    state = ProxyState(
        phase=Phase.FETCH,
        hosts=FETCH_HOSTS,
        generation=0,
        tunnels={tunnel},
        test_routes={("fixture-git", 443): "172.17.0.1"},
    )

    _apply_allowlist(state, Phase.INSTALL, frozenset({"pypi.org"}), 1)

    assert state.phase is Phase.INSTALL
    assert state.generation == 1
    assert state.tunnels == set()
    assert state.test_routes == {}
    assert client.fileno() == -1
    assert upstream.fileno() == -1
    with pytest.raises(ValueError):
        _apply_allowlist(state, Phase.INSTALL, frozenset({"pypi.org"}), 1)
    with pytest.raises(ValueError):
        _apply_allowlist(state, Phase.FETCH, FETCH_HOSTS, 2)


def test_control_round_trip(tmp_path: Path) -> None:
    path = str(tmp_path / "control.sock")
    state = ProxyState(phase=Phase.FETCH, hosts=FETCH_HOSTS, generation=0)
    stop = threading.Event()
    thread = threading.Thread(
        target=_serve_control, args=(state, path, stop), daemon=True
    )
    thread.start()
    _wait_for_control(path)

    response = _send_control(
        path,
        {
            "action": "allow",
            "phase": "install",
            "hosts": ["pypi.org"],
            "generation": 1,
        },
    )
    assert response == {"ok": True}
    assert state.phase is Phase.INSTALL
    assert state.hosts == frozenset({"pypi.org"})
    assert state.generation == 1

    refused = _send_control(
        path,
        {
            "action": "allow",
            "phase": "install",
            "hosts": ["github.com"],
            "generation": 2,
        },
    )
    assert refused["ok"] is False

    backwards = _send_control(
        path,
        {
            "action": "allow",
            "phase": "fetch",
            "hosts": ["github.com"],
            "generation": 2,
        },
    )
    assert backwards["ok"] is False

    stop.set()
    thread.join(timeout=2)


def test_handle_control_test_routes() -> None:
    state = ProxyState(phase=Phase.INSTALL, hosts=frozenset({"pypi.org"}), generation=0)
    _handle_control(
        state,
        {
            "action": "test-routes",
            "routes": {"fixture-git:8443": "172.17.0.1"},
        },
    )
    assert state.test_routes == {("fixture-git", 8443): "172.17.0.1"}
    with pytest.raises(ValueError):
        _handle_control(
            state,
            {"action": "test-routes", "routes": {"localhost:8443": "127.0.0.1"}},
        )
    with pytest.raises(ValueError):
        _handle_control(
            state,
            {
                "action": "test-routes",
                "routes": {"fixture-git:8443": "not-an-ip"},
            },
        )


def test_log_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    state = ProxyState(phase=Phase.INSTALL, hosts=frozenset({"pypi.org"}), generation=0)
    _log(
        state,
        {
            "event": "request",
            "time": "2026-11-18T00:00:00.000Z",
            "phase": "install",
            "host": "github.com",
            "port": 443,
            "decision": "deny",
            "reason": "not-allowlisted",
        },
    )
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["reason"] == "not-allowlisted"


def test_proxy_tunnels_to_test_route() -> None:
    echo = _EchoServer()
    control_path = _control_path()
    proxy_port = _free_port()
    stop = threading.Event()
    thread = threading.Thread(
        target=serve,
        kwargs={
            "port": proxy_port,
            "phase": Phase.FETCH,
            "hosts": FETCH_HOSTS,
            "control_socket": control_path,
            "stop_event": stop,
        },
        daemon=True,
    )
    thread.start()
    try:
        _wait_for_control(control_path)
        _send_control(
            control_path,
            {
                "action": "test-routes",
                "routes": {f"fixture-echo.test:{echo.port}": "127.0.0.1"},
            },
        )

        deadline = time.monotonic() + 5
        client: socket.socket | None = None
        while time.monotonic() < deadline:
            try:
                client = socket.create_connection(("127.0.0.1", proxy_port), timeout=2)
                break
            except OSError:
                time.sleep(0.02)
        assert client is not None

        with client:
            client.sendall(
                f"CONNECT fixture-echo.test:{echo.port} HTTP/1.1\r\n\r\n".encode()
            )
            response = client.recv(4096)
            assert b"200" in response
            client.sendall(b"hello")
            assert client.recv(5) == b"hello"
    finally:
        echo.close()
        stop.set()
        thread.join(timeout=2)


def test_handle_client_allows_allowlisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProxyState(phase=Phase.INSTALL, hosts=frozenset({"pypi.org"}), generation=0)
    client, peer = socket.socketpair()
    upstream, upstream_peer = socket.socketpair()
    monkeypatch.setattr(
        egress_proxy,
        "decide",
        lambda *args, **kwargs: Decision(
            True, "allowlisted", "pypi.org", 443, ("8.8.8.8",)
        ),
    )
    monkeypatch.setattr(egress_proxy, "_connect", lambda *args: upstream)
    thread = threading.Thread(target=_handle_client, args=(state, client), daemon=True)
    thread.start()
    try:
        peer.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        assert b"200" in peer.recv(4096)
        peer.sendall(b"hello")
        assert upstream_peer.recv(5) == b"hello"
        upstream_peer.sendall(b"world")
        assert peer.recv(5) == b"world"
    finally:
        peer.close()
        upstream_peer.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_malformed_connect_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    client, peer = socket.socketpair()
    state = ProxyState(phase=Phase.INSTALL, hosts=frozenset(), generation=0)
    thread = threading.Thread(target=_handle_client, args=(state, client), daemon=True)
    thread.start()
    try:
        peer.sendall(b"BAD\r\n\r\n")
        assert b"400" in peer.recv(4096)
    finally:
        peer.close()
    thread.join(timeout=2)
    assert '"reason":"bad-request"' in capsys.readouterr().out


def test_allow_survives_a_lost_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = EgressProxy(
        container="unit",
        run_id="unit",
        deadline=Deadline(10),
        internal_network="internal",
        egress_network="egress",
        phase=Phase.FETCH,
    )
    calls: list[dict[str, object]] = []

    def control(action: str, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise SetupFailed("allowlist-failed", "lost ack")
        return {"ok": True}

    monkeypatch.setattr(proxy, "_control", control)
    with pytest.raises(SetupFailed):
        proxy.allow(Phase.INSTALL, ["pypi.org"])
    assert proxy.generation == 1
    assert proxy.phase is Phase.FETCH
    proxy.allow(Phase.INSTALL, ["pypi.org"])
    assert calls[-1]["generation"] == 2
    assert proxy.phase.value == "install"


def test_allow_cannot_go_back_to_fetch() -> None:
    proxy = EgressProxy(
        container="unit",
        run_id="unit",
        deadline=Deadline(10),
        internal_network="internal",
        egress_network="egress",
        phase=Phase.INSTALL,
    )
    with pytest.raises(ValueError):
        proxy.allow(Phase.FETCH, ["github.com"])
