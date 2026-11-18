from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest

import bugrepro
from bugrepro import main

Reply = Callable[[dict[str, object], dict[str, str]], tuple[int, dict[str, object]]]


class _Provider:
    """A local OpenAI-compatible endpoint that records every request."""

    def __init__(self, reply: Reply) -> None:
        self.requests: list[dict[str, object]] = []
        self.headers: list[dict[str, str]] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = cast(dict[str, object], json.loads(self.rfile.read(length)))
                provider.requests.append(body)
                provider.headers.append(dict(self.headers.items()))
                status, payload = reply(body, dict(self.headers.items()))
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        port = int(self.server.server_address[1])
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _success(text: str) -> tuple[int, dict[str, object]]:
    prompt = 10
    completion = 5
    return 200, {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "DeepSeek-V4.1-Flash-1017",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": prompt,
        },
    }


def _ok(
    body: dict[str, object], headers: dict[str, str]
) -> tuple[int, dict[str, object]]:
    return _success("hello")


@contextmanager
def _provider(reply: Reply) -> Iterator[_Provider]:
    provider = _Provider(reply)
    try:
        yield provider
    finally:
        provider.close()


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    key: str = "test-key-not-real",
) -> None:
    monkeypatch.setenv("BUGREPRO_BASE_URL", provider.url)
    monkeypatch.setenv("BUGREPRO_MODEL", "deepseek-flash")
    monkeypatch.setenv("BUGREPRO_API_KEY", key)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(bugrepro, "ensure_supported_docker", lambda: None)


def test_command_runs() -> None:
    result = subprocess.run(["bugrepro"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "sandbox" in result.stdout


def test_cost_cap_and_default_are_in_help() -> None:
    result = subprocess.run(
        ["bugrepro", "model", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--cost-cap" in result.stdout
    assert "USD" in result.stdout
    assert "1.00" in result.stdout


def test_model_run_logs_each_call_cost_and_the_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "test-key-not-real"
    with _provider(_ok) as provider:
        _configure(monkeypatch, provider, canary)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()

    assert code == 0
    log = json.loads(out)
    assert log["outcome"] == "ok"
    assert log["model"] == "DeepSeek-V4.1-Flash-1017"
    assert log["cost_cap_usd"] == 1.0
    assert len(log["calls"]) == 1
    assert log["cost_usd"] == sum(call["cost_usd"] for call in log["calls"])
    assert log["finish_reason"] == "stop"
    assert provider.requests[0]["model"] == "deepseek-flash"
    assert provider.requests[0]["max_tokens"] == 4096
    assert provider.headers[0]["Authorization"] == f"Bearer {canary}"
    assert canary not in out
    assert canary not in err


def test_low_cost_cap_stops_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _provider(_ok) as provider:
        _configure(monkeypatch, provider)
        code = main(["model", "--prompt", "hello", "--cost-cap", "0.000001"])
        out, err = capsys.readouterr()

    assert code == 1
    log = json.loads(out)
    assert log["outcome"] == "stopped-at-cost-cap"
    assert log["cost_cap_usd"] == 0.000001
    assert log["cost_usd"] == 0
    assert log["calls"] == []
    assert provider.requests == []
    assert err == ""


def test_provider_error_stops_after_three_charged_attempts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def overloaded(
        body: dict[str, object], headers: dict[str, str]
    ) -> tuple[int, dict[str, object]]:
        return 503, {"error": {"message": "overloaded"}}

    with _provider(overloaded) as provider:
        _configure(monkeypatch, provider)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()

    assert code == 1
    log = json.loads(out)
    assert log["outcome"] == "error"
    assert "overloaded" in log["error"]
    assert len(provider.requests) == 3
    assert len(log["calls"]) == 3
    assert all(call["kind"] == "worst-case" for call in log["calls"])
    assert log["cost_usd"] == sum(call["cost_usd"] for call in log["calls"])
    assert log["cost_usd"] <= log["cost_cap_usd"]
    assert err == ""


def test_api_key_appears_in_no_log_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "test-key-not-real"

    def echo_key(
        body: dict[str, object], headers: dict[str, str]
    ) -> tuple[int, dict[str, object]]:
        return 401, {
            "error": {"message": f"rejected {headers.get('Authorization', '')}"}
        }

    with _provider(echo_key) as provider:
        _configure(monkeypatch, provider, canary)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()

    assert code == 1
    log = json.loads(out)
    assert log["outcome"] == "error"
    assert "rejected" in log["error"]
    assert len(provider.requests) == 1
    assert canary not in out
    assert canary not in err

    def echo_key_success(
        body: dict[str, object], headers: dict[str, str]
    ) -> tuple[int, dict[str, object]]:
        return _success(f"echo {headers.get('Authorization', '')}")

    with _provider(echo_key_success) as success_provider:
        _configure(monkeypatch, success_provider, canary)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()

    assert code == 0
    assert json.loads(out)["text"] == "echo Bearer [redacted]"
    assert canary not in out
    assert canary not in err

    newline_key = "canary-secret-with-newline\nInjected: yes"
    with _provider(_ok) as newline_provider:
        _configure(monkeypatch, newline_provider, newline_key)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()
        assert newline_provider.requests == []

    assert code == 2
    assert newline_key not in out
    assert newline_key not in err
    assert "control character" in err

    boundary_key = "test-key-not-real"

    def boundary_error(
        body: dict[str, object], headers: dict[str, str]
    ) -> tuple[int, dict[str, object]]:
        return 401, {"error": {"message": "x" * 490 + boundary_key}}

    with _provider(boundary_error) as boundary_provider:
        _configure(monkeypatch, boundary_provider, boundary_key)
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()
        assert len(boundary_provider.requests) == 1

    assert code == 1
    assert boundary_key not in out
    assert boundary_key[:10] not in out
    assert boundary_key not in err


def test_unpriced_model_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _provider(_ok) as provider:
        monkeypatch.setenv("BUGREPRO_BASE_URL", provider.url)
        monkeypatch.setenv("BUGREPRO_MODEL", "deepseek-unpriced")
        monkeypatch.setenv("BUGREPRO_API_KEY", "test-key-not-real")
        code = main(["model", "--prompt", "hello"])
        out, err = capsys.readouterr()
        assert provider.requests == []

    assert code == 2
    assert out == ""
    assert "no price entry" in err


def test_missing_api_key_fails_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BUGREPRO_API_KEY", raising=False)
    monkeypatch.setenv("BUGREPRO_BASE_URL", "http://127.0.0.1:1")

    code = main(["model", "--prompt", "hello"])
    out, err = capsys.readouterr()

    assert code == 2
    assert out == ""
    assert "BUGREPRO_API_KEY" in err
