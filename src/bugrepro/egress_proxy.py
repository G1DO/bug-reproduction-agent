"""An allowlisting HTTP CONNECT proxy for the sandbox's setup phase.

The proxy runs in its own container, on the run's internal network. Setup
containers have no other route out. It allows a hostname only when the current
phase's allowlist contains it, refuses IP literals, resolves each name once,
and connects only to an address that is public unicast.

The module is also its own control client. The host changes the allowlist with
``python egress_proxy.py control`` inside the proxy container, over a Unix
socket. No control listener exists on the network.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast

PROXY_PORT: Final = 3128
CONTROL_SOCKET: Final = "/tmp/bugrepro-egress.sock"
MAX_REQUEST_BYTES: Final = 8192
MAX_CONTROL_BYTES: Final = 1_048_576
MAX_LOG_LINES: Final = 10_000
CONNECT_TIMEOUT_S: Final = 10.0
READ_SIZE: Final = 65_536

FETCH_HOSTS: Final[frozenset[str]] = frozenset({"github.com"})


class Phase(StrEnum):
    """The setup phase that determines the allowlist."""

    FETCH = "fetch"
    INSTALL = "install"
    TEST = "test"


@dataclass(frozen=True)
class Decision:
    """The proxy's answer to one CONNECT request."""

    allowed: bool
    reason: str
    host: str
    port: int
    addresses: tuple[str, ...] = ()


@dataclass(eq=False)
class Tunnel:
    """One open client-to-upstream tunnel."""

    client: socket.socket
    upstream: socket.socket
    generation: int


@dataclass
class ProxyState:
    """Mutable state shared by the listener, tunnels and control socket."""

    phase: Phase
    hosts: frozenset[str]
    generation: int
    tunnels: set[Tunnel] = field(default_factory=set)
    test_routes: dict[tuple[str, int], str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    log_count: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log(state: ProxyState, event: dict[str, object]) -> None:
    """Write one JSON log line, stopping at the cap."""
    with state.lock:
        if state.log_count >= MAX_LOG_LINES:
            if state.log_count == MAX_LOG_LINES:
                state.log_count += 1
                print(json.dumps({"event": "log-truncated"}), flush=True)
            return
        state.log_count += 1
        print(json.dumps(event, separators=(",", ":")), flush=True)


def normalize_host(raw: str) -> str:
    """Return a lower-case ASCII hostname or raise ``ValueError``."""
    host = raw.strip().lower().removesuffix(".")
    if not host or len(host) > 253:
        raise ValueError("invalid host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid host") from exc
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("invalid host")
    labels = host.split(".")
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid host")
    if any(label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("invalid host")
    return host


def is_ip_literal(host: str) -> bool:
    """Return whether ``host`` is an IPv4 or IPv6 literal, with or without brackets."""
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def is_public_unicast(address: str) -> bool:
    """Return whether an address is a routable, public unicast address.

    ``ipaddress.is_global`` alone is not enough: it reports multicast addresses
    such as 224.0.0.1 and ff02::1 as global.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_link_local:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return False
    return ip.is_global


def parse_connect(line: str) -> tuple[str, int]:
    """Parse ``CONNECT host:port HTTP/1.x`` into its host and port."""
    parts = line.rstrip("\r\n").split(" ")
    if len(parts) != 3:
        raise ValueError("malformed request line")
    method, target, version = parts
    if method != "CONNECT":
        raise ValueError("only CONNECT is supported")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise ValueError("unsupported HTTP version")
    if target.startswith("["):
        end = target.find("]")
        if end == -1 or not target[end + 1 :].startswith(":"):
            raise ValueError("malformed target")
        host = target[1:end]
        port_text = target[end + 2 :]
    else:
        if target.count(":") != 1:
            raise ValueError("malformed target")
        host, port_text = target.split(":", 1)
    if not host or not port_text.isdigit():
        raise ValueError("malformed target")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return host, port


def resolve_once(host: str, port: int) -> tuple[str, ...]:
    """Resolve a name once and return its deduplicated addresses."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if isinstance(address, str):
            address = address.split("%", 1)[0]
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise OSError("no addresses")
    return tuple(addresses)


def decide(
    host: str,
    port: int,
    hosts: frozenset[str],
    test_routes: dict[tuple[str, int], str] | None = None,
) -> Decision:
    """Decide one CONNECT request without opening a connection."""
    try:
        normalized = normalize_host(host)
    except ValueError:
        return Decision(False, "bad-host", host, port)
    if test_routes is not None and (normalized, port) in test_routes:
        return Decision(
            True,
            "test-override",
            normalized,
            port,
            (test_routes[(normalized, port)],),
        )
    if is_ip_literal(host):
        return Decision(False, "ip-literal", normalized, port)
    if port != 443:
        return Decision(False, "port-not-allowed", normalized, port)
    if normalized not in hosts:
        return Decision(False, "not-allowlisted", normalized, port)
    try:
        addresses = resolve_once(normalized, port)
    except OSError:
        return Decision(False, "resolve-failed", normalized, port)
    if not all(is_public_unicast(address) for address in addresses):
        return Decision(False, "non-global-address", normalized, port, addresses)
    return Decision(True, "allowlisted", normalized, port, addresses)


def validate_policy(phase: Phase, hosts: Iterable[str]) -> frozenset[str]:
    """Return a normalized allowlist, refusing policies the phases forbid."""
    normalized = frozenset(normalize_host(host) for host in hosts)
    if phase is Phase.FETCH and normalized != FETCH_HOSTS:
        raise ValueError("fetch allows github.com only")
    if phase is Phase.INSTALL and "github.com" in normalized:
        raise ValueError("github.com is not allowed during install")
    if phase is Phase.TEST:
        raise ValueError("the test phase has no network")
    return normalized


def _send_response(client: socket.socket, status: str) -> None:
    try:
        client.sendall(
            f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode()
        )
    except OSError:
        pass


def _read_request(client: socket.socket) -> str:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < MAX_REQUEST_BYTES:
        chunk = client.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    if b"\r\n\r\n" not in data:
        raise ValueError("incomplete request")
    head = bytes(data).split(b"\r\n\r\n", 1)[0]
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("non-ascii request") from exc
    return text.split("\r\n", 1)[0]


def _close_tunnel(tunnel: Tunnel) -> None:
    for sock in (tunnel.client, tunnel.upstream):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _connect(addresses: tuple[str, ...], port: int) -> socket.socket | None:
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect((address, port))
            sock.settimeout(None)
            return sock
        except OSError:
            sock.close()
    return None


def _pipe(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            data = source.recv(READ_SIZE)
            if not data:
                break
            destination.sendall(data)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_client(state: ProxyState, client: socket.socket) -> None:
    tunnel: Tunnel | None = None
    try:
        client.settimeout(CONNECT_TIMEOUT_S)
        line = _read_request(client)
        try:
            host, port = parse_connect(line)
        except ValueError:
            _log(
                state,
                {
                    "event": "request",
                    "time": _now(),
                    "phase": state.phase.value,
                    "host": "",
                    "port": 0,
                    "decision": "deny",
                    "reason": "bad-request",
                },
            )
            _send_response(client, "400 Bad Request")
            return

        with state.lock:
            generation = state.generation
            hosts = state.hosts
            test_routes = dict(state.test_routes)

        decision = decide(host, port, hosts, test_routes)
        _log(
            state,
            {
                "event": "request",
                "time": _now(),
                "phase": state.phase.value,
                "host": decision.host,
                "port": decision.port,
                "decision": "allow" if decision.allowed else "deny",
                "reason": decision.reason,
                "addresses": list(decision.addresses),
            },
        )
        if not decision.allowed:
            _send_response(client, "403 Forbidden")
            return

        upstream = _connect(decision.addresses, decision.port)
        if upstream is None:
            _log(
                state,
                {
                    "event": "connect-failed",
                    "time": _now(),
                    "phase": state.phase.value,
                    "host": decision.host,
                    "port": decision.port,
                    "addresses": list(decision.addresses),
                },
            )
            _send_response(client, "502 Bad Gateway")
            return

        with state.lock:
            if state.generation != generation:
                upstream.close()
                _send_response(client, "403 Forbidden")
                return
            tunnel = Tunnel(client=client, upstream=upstream, generation=generation)
            state.tunnels.add(tunnel)

        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        client.settimeout(None)
        first = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        second = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        first.start()
        second.start()
        first.join()
        second.join()
    except (OSError, ValueError):
        _send_response(client, "400 Bad Request")
    finally:
        if tunnel is not None:
            with state.lock:
                state.tunnels.discard(tunnel)
            _close_tunnel(tunnel)
        else:
            try:
                client.close()
            except OSError:
                pass


def _apply_allowlist(
    state: ProxyState,
    phase: Phase,
    hosts: frozenset[str],
    generation: int,
) -> None:
    """Switch phases and close every tunnel from an older generation."""
    with state.lock:
        if generation <= state.generation:
            raise ValueError("generation must increase")
        state.phase = phase
        state.hosts = hosts
        state.generation = generation
        tunnels = list(state.tunnels)
        state.tunnels.clear()
    _log(
        state,
        {
            "event": "allowlist",
            "time": _now(),
            "phase": phase.value,
            "hosts": sorted(hosts),
            "generation": generation,
        },
    )
    for tunnel in tunnels:
        _close_tunnel(tunnel)


def _recv_json(conn: socket.socket) -> dict[str, object]:
    data = bytearray()
    while b"\n" not in data and len(data) < MAX_CONTROL_BYTES:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    if b"\n" not in data:
        raise ValueError("incomplete control message")
    parsed: object = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(parsed, dict):
        raise TypeError("control message must be an object")
    return cast(dict[str, object], parsed)


def _handle_control(state: ProxyState, message: dict[str, object]) -> None:
    action = message.get("action")
    if action == "ping":
        return
    if action == "allow":
        phase_raw = message.get("phase")
        hosts_raw = message.get("hosts")
        generation_raw = message.get("generation")
        if (
            not isinstance(phase_raw, str)
            or not isinstance(hosts_raw, list)
            or not isinstance(generation_raw, int)
        ):
            raise ValueError("invalid allow message")
        if not all(isinstance(host, str) for host in hosts_raw):
            raise ValueError("invalid host")
        phase = Phase(phase_raw)
        hosts = validate_policy(phase, cast(list[str], hosts_raw))
        _apply_allowlist(state, phase, hosts, generation_raw)
        return
    if action == "test-routes":
        routes_raw = message.get("routes")
        if not isinstance(routes_raw, dict):
            raise ValueError("invalid routes")
        routes: dict[tuple[str, int], str] = {}
        for key, address in routes_raw.items():
            if not isinstance(key, str) or not isinstance(address, str):
                raise TypeError("invalid route")
            host, separator, port_text = key.rpartition(":")
            if not separator or not port_text.isdigit():
                raise ValueError("invalid route")
            routes[(normalize_host(host), int(port_text))] = address
        with state.lock:
            state.test_routes = routes
        return
    raise ValueError("unknown action")


def _serve_control(state: ProxyState, path: str, stop: threading.Event) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(4)
    server.settimeout(0.5)
    try:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                try:
                    message = _recv_json(conn)
                    _handle_control(state, message)
                    conn.sendall(b'{"ok":true}\n')
                except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
                    try:
                        payload = json.dumps({"ok": False, "error": str(exc)})
                        conn.sendall(payload.encode() + b"\n")
                    except OSError:
                        pass
    finally:
        server.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _send_control(path: str, message: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(30)
        conn.connect(path)
        conn.sendall(json.dumps(message).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data and len(data) < MAX_CONTROL_BYTES:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
    if b"\n" not in data:
        raise OSError("the proxy did not answer")
    parsed: object = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(parsed, dict):
        raise OSError("invalid proxy response")
    return cast(dict[str, object], parsed)


def serve(
    *,
    port: int,
    phase: Phase,
    hosts: Iterable[str],
    control_socket: str,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the proxy until the process is stopped."""
    normalized = validate_policy(phase, hosts)
    state = ProxyState(phase=phase, hosts=normalized, generation=0)
    stop = stop_event or threading.Event()
    control = threading.Thread(
        target=_serve_control, args=(state, control_socket, stop), daemon=True
    )
    control.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    server.settimeout(0.5)
    _log(
        state,
        {
            "event": "ready",
            "time": _now(),
            "phase": phase.value,
            "hosts": sorted(normalized),
            "port": port,
            "generation": 0,
        },
    )
    try:
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            threading.Thread(
                target=_handle_client, args=(state, client), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.close()
        with state.lock:
            tunnels = list(state.tunnels)
            state.tunnels.clear()
        for tunnel in tunnels:
            _close_tunnel(tunnel)
        control.join(timeout=2)


def _parse_route(value: str) -> tuple[str, str]:
    key, separator, address = value.partition("=")
    if not separator or not key or not address:
        raise argparse.ArgumentTypeError("route must be HOST:PORT=ADDRESS")
    return key, address


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bugrepro-egress")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="Run the proxy.")
    serve_parser.add_argument("--port", type=int, default=PROXY_PORT)
    serve_parser.add_argument(
        "--phase", choices=[phase.value for phase in Phase], default=Phase.FETCH.value
    )
    serve_parser.add_argument("--host", action="append", default=[])
    serve_parser.add_argument("--control-socket", default=CONTROL_SOCKET)

    control_parser = commands.add_parser(
        "control", help="Change the running proxy's allowlist."
    )
    control_parser.add_argument("--socket", default=CONTROL_SOCKET)
    control_parser.add_argument(
        "--action", choices=("allow", "ping", "test-routes"), required=True
    )
    control_parser.add_argument("--phase", choices=[phase.value for phase in Phase])
    control_parser.add_argument("--generation", type=int)
    control_parser.add_argument("--host", action="append", default=[])
    control_parser.add_argument("--route", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        serve(
            port=args.port,
            phase=Phase(args.phase),
            hosts=args.host,
            control_socket=args.control_socket,
        )
        return 0
    if args.command == "control":
        message: dict[str, object] = {"action": args.action}
        if args.phase is not None:
            message["phase"] = args.phase
        if args.generation is not None:
            message["generation"] = args.generation
        if args.host:
            message["hosts"] = args.host
        if args.route:
            message["routes"] = dict(_parse_route(route) for route in args.route)
        try:
            response = _send_control(args.socket, message)
        except (OSError, ValueError) as exc:
            print(f"bugrepro-egress: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(response))
        return 0 if response.get("ok") is True else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
