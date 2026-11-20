from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import bugrepro.verify as verify_module
from bugrepro.languages.python import CHECKOUT
from bugrepro.model import Budget, ModelClient, ModelResult, Outcome
from bugrepro.reproduce import ReproductionTest, reproduce_issue
from bugrepro.sandbox import (
    PROXY_IMAGE,
    CommandResult,
    Deadline,
    Sandbox,
    SandboxError,
    SetupImage,
    cleanup,
    new_run_id,
)
from bugrepro.verify import (
    ERROR_MATCH_MATCHED,
    ERROR_MATCH_NOT_MATCHED,
    ERROR_MATCH_SKIPPED,
    NO_ERROR_IN_REPORT,
    OUTCOME_COULD_NOT_REPRODUCE,
    OUTCOME_ERROR,
    OUTCOME_REPRODUCED,
    OUTCOME_STOPPED_AT_TIME_LIMIT,
    VerifyResult,
    verify_test,
)

TITLE = "Parser drops the last line"
REPORT = "The parser raises:\n```\nValueError: bad value\n```\n"
IMAGE = SetupImage("fake-setup", "run-1")
TEST = ReproductionTest(
    path="tests/test_repro.py",
    content="def test_repro():\n    parser.parse('x')\n",
    command=(
        "/tmp/venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "tests/test_repro.py",
    ),
)


def _failed(output: str, *, exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code, output, "", 0.01)


def _traceback() -> str:
    return (
        "tests/test_repro.py:2: in test_repro\n"
        "    parser.parse('x')\n"
        "src/parser.py:9: in parse\n"
        "    raise ValueError('bad value')\n"
        "E   ValueError: bad value\n"
        "1 failed in 0.01s\n"
    )


def _assertion_failure() -> str:
    return (
        "tests/test_repro.py:2: in test_repro\n"
        "    assert parser.parse('x') == 2\n"
        "E   AssertionError: assert 1 == 2\n"
        "1 failed in 0.01s\n"
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    results: Sequence[CommandResult],
    *,
    report: str = REPORT,
    test: ReproductionTest = TEST,
    label: str | None = None,
) -> tuple[
    VerifyResult, list[tuple[SetupImage, tuple[str, ...], float, str, Deadline, str]]
]:
    remaining = list(results)
    calls: list[tuple[SetupImage, tuple[str, ...], float, str, Deadline, str]] = []
    deadline = Deadline(60)

    def fake_run_isolated(
        image: SetupImage,
        command: Sequence[str],
        *,
        timeout: float,
        run_id: str,
        deadline: Deadline,
        workdir: str = "",
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del env
        calls.append((image, tuple(command), timeout, run_id, deadline, workdir))
        return remaining.pop(0)

    monkeypatch.setattr(verify_module, "run_isolated", fake_run_isolated)
    result = verify_test(
        IMAGE,
        test,
        report=report,
        run_id="run-1",
        deadline=deadline,
        label=label,
    )
    return result, calls


def test_three_failing_runs_with_the_report_error_are_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run(monkeypatch, [_failed(_traceback()) for _ in range(3)])

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.reproduced
    assert result.test is TEST
    assert result.error_match.status == ERROR_MATCH_MATCHED
    assert result.error_match.pattern == "ValueError: bad value"
    assert len(result.steps) == 3
    assert len(calls) == 3
    assert all(call[0] == IMAGE for call in calls)
    assert all(call[1] == TEST.command for call in calls)
    assert all(call[2] > 0 for call in calls)
    assert all(call[3] == "run-1" for call in calls)
    assert all(call[5] == CHECKOUT for call in calls)


def test_error_type_alone_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _run(
        monkeypatch,
        [_failed(_traceback()) for _ in range(3)],
        report="It raises ValueError.",
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is TEST
    assert result.error_match.status == ERROR_MATCH_MATCHED
    assert result.error_match.pattern == "ValueError"


def test_two_of_three_failures_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run(
        monkeypatch,
        [
            _failed(_traceback()),
            _failed(_traceback()),
            CommandResult(0, "1 passed in 0.01s\n", "", 0.01),
        ],
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-passed"
    assert result.test is None
    assert len(result.steps) == 3
    assert len(calls) == 3


def test_failure_without_the_report_error_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _run(
        monkeypatch,
        [_failed(_assertion_failure()) for _ in range(3)],
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None
    assert result.error_match.status == ERROR_MATCH_NOT_MATCHED


def test_test_that_raises_the_report_message_itself_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raising_test = ReproductionTest(
        path=TEST.path,
        content=("def test_repro():\n    raise ValueError('ValueError: bad value')\n"),
        command=TEST.command,
    )
    output = (
        "tests/test_repro.py:2: in test_repro\n"
        "    raise ValueError('ValueError: bad value')\n"
        "E   ValueError: ValueError: bad value\n"
        "1 failed in 0.01s\n"
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=raising_test,
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-message-in-test"
    assert result.test is None


def test_failure_raised_in_the_test_file_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raising_test = ReproductionTest(
        path=TEST.path,
        content="def test_repro():\n    raise ValueError('boom')\n",
        command=TEST.command,
    )
    output = (
        "tests/test_repro.py:2: in test_repro\n"
        "    raise ValueError('boom')\n"
        "E   ValueError: boom\n"
        "1 failed in 0.01s\n"
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report="It raises ValueError.",
        test=raising_test,
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_report_without_an_error_message_skips_the_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _run(
        monkeypatch,
        [_failed(_assertion_failure()) for _ in range(3)],
        report="The parser drops the last line.",
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is TEST
    assert result.error_match.status == ERROR_MATCH_SKIPPED
    assert result.error_match.reason == NO_ERROR_IN_REPORT
    assert result.log()["error_match"] == {
        "status": ERROR_MATCH_SKIPPED,
        "reason": NO_ERROR_IN_REPORT,
        "pattern": None,
    }


def test_collection_error_is_not_a_reproduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        "ERROR collecting tests/test_repro.py\nImportError: no module named parser\n"
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output, exit_code=2) for _ in range(3)],
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_docker_failure_is_an_error_and_the_test_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
    result, _ = _run(monkeypatch, [_failed(output, exit_code=125)])

    assert result.outcome == OUTCOME_ERROR
    assert result.reason == "sandbox-error"
    assert result.test is None
    assert result.error is not None
    assert "Cannot connect to the Docker daemon" in result.error
    assert len(result.steps) == 1


def test_timed_out_run_stops_without_returning_the_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _run(
        monkeypatch,
        [CommandResult(124, "partial output", "", 0.01, timed_out=True)],
    )

    assert result.outcome == OUTCOME_STOPPED_AT_TIME_LIMIT
    assert result.reason == "stopped-at-time-limit"
    assert result.test is None
    assert len(result.steps) == 1
    assert result.steps[0].timed_out


def test_sandbox_exception_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run(*args: object, **kwargs: object) -> CommandResult:
        del args, kwargs
        raise SandboxError("docker was not found on PATH")

    monkeypatch.setattr(verify_module, "run_isolated", failing_run)
    result = verify_test(
        IMAGE,
        TEST,
        report=REPORT,
        run_id="run-1",
        deadline=Deadline(60),
    )

    assert result.outcome == OUTCOME_ERROR
    assert result.reason == "sandbox-error"
    assert result.test is None
    assert result.error == "docker was not found on PATH"


def test_could_not_reproduce_report_lists_every_step_with_its_command_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = [_assertion_failure(), _assertion_failure(), _assertion_failure()]
    result, _ = _run(monkeypatch, [_failed(output) for output in outputs])

    log = result.log()
    steps = cast(list[dict[str, object]], log["steps"])
    assert len(steps) == 3
    for step, output in zip(steps, outputs, strict=True):
        assert step["kind"] == "verify"
        assert step["command"] == list(TEST.command)
        assert step["output"] == output.rstrip("\n")
        assert step["exit_code"] == 1
        assert isinstance(step["start"], str)
        assert step["start"]
        assert step["duration_s"] == 0.01


def test_result_line_carries_a_label_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain, _ = _run(monkeypatch, [_failed(_traceback()) for _ in range(3)])
    labelled, _ = _run(
        monkeypatch,
        [_failed(_traceback()) for _ in range(3)],
        label="unverified",
    )

    assert "label" not in plain.log()
    assert labelled.log()["label"] == "unverified"


class _ScriptedModel:
    def __init__(self, results: Sequence[ModelResult], *, budget: Budget) -> None:
        self.results = list(results)
        self.budget = budget

    def complete(
        self,
        messages: Sequence[dict[str, object]],
        *,
        max_tokens: int,
        deadline: Deadline,
    ) -> ModelResult:
        del messages, max_tokens, deadline
        if not self.results:
            return ModelResult(Outcome.ERROR, error="no scripted reply")
        return self.results.pop(0)


def _action(tool: str, **arguments: object) -> ModelResult:
    return ModelResult(Outcome.OK, text=json.dumps({"tool": tool, **arguments}))


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_existing.py").write_text("def test_existing():\n    assert True\n")
    return project


_SHIM = """#!/usr/bin/env python
import socket
import sys
from pathlib import Path

path = Path("/tmp/checkout") / sys.argv[-1]
print("TEST-CONTENT:" + path.read_text())
print("HOSTNAME:" + socket.gethostname())
try:
    socket.create_connection(("example.com", 443), 2)
except OSError:
    print("NETWORK:blocked")
else:
    print("NETWORK:connected")
print("tests/test_repro.py:2: in test_repro")
print("    parser.parse('x')")
print("src/parser.py:9: in parse")
print("    raise ValueError('bad value')")
print("E   ValueError: bad value")
print("1 failed in 0.01s")
raise SystemExit(1)
"""


@pytest.mark.docker
def test_scripted_model_test_is_verified_in_three_new_no_network_containers(
    tmp_path: Path,
) -> None:
    run_id = new_run_id()
    deadline = Deadline(300)
    sandbox = Sandbox.start(PROXY_IMAGE, run_id=run_id, deadline=deadline, role="setup")
    result: VerifyResult | None = None
    try:
        sandbox.copy_in(_project(tmp_path), CHECKOUT)
        install = sandbox.run(
            [
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    "p = Path('/tmp/venv/bin/python'); "
                    "p.parent.mkdir(parents=True); "
                    f"p.write_text({_SHIM!r}); "
                    "p.chmod(0o755)"
                ),
            ]
        )
        assert install.ok, install.stderr
        budget = Budget(cap_usd=Decimal(1))
        model = _ScriptedModel(
            [
                _action("write_test", path=TEST.path, content=TEST.content),
                _action("done"),
            ],
            budget=budget,
        )
        reproduced = reproduce_issue(
            sandbox,
            budget,
            issue_title=TITLE,
            issue_body=REPORT,
            model=cast(ModelClient, model),
        )
        assert reproduced.handed_to_verification
        assert reproduced.image is not None
        assert reproduced.test is not None
        result = verify_test(
            reproduced.image,
            reproduced.test,
            report=REPORT,
            run_id=run_id,
            deadline=deadline,
        )
    finally:
        cleanup(run_id)

    assert result is not None
    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert len(result.steps) == 3
    hosts: list[str] = []
    for step in result.steps:
        assert "TEST-CONTENT:" + TEST.content in step.output
        assert "NETWORK:blocked" in step.output
        host = next(
            line.split(":", 1)[1]
            for line in step.output.splitlines()
            if line.startswith("HOSTNAME:")
        )
        hosts.append(host)
    assert len(set(hosts)) == 3
