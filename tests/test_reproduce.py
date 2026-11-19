from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bugrepro.languages.python import CHECKOUT
from bugrepro.model import Budget, ModelClient, ModelResult, Outcome
from bugrepro.reproduce import (
    OUTCOME_COULD_NOT_REPRODUCE,
    OUTCOME_ERROR,
    OUTCOME_HANDED_TO_VERIFICATION,
    OUTCOME_STOPPED_AT_COST_CAP,
    OUTCOME_STOPPED_AT_TIME_LIMIT,
    ReproduceResult,
    reproduce_issue,
)
from bugrepro.sandbox import (
    PROXY_IMAGE,
    CommandResult,
    Deadline,
    Sandbox,
    SetupImage,
    cleanup,
    new_run_id,
)

TITLE = "Parser drops the last line"
BODY = "When the input ends with a newline, the parser raises ValueError."


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "", 0.01)


def _failed(stderr: str) -> CommandResult:
    return CommandResult(1, "", stderr, 0.01)


class FakeSandbox:
    def __init__(
        self, handler: Callable[[Sequence[str]], CommandResult] | None = None
    ) -> None:
        self.deadline = Deadline(120)
        self.run_id = new_run_id()
        self.commands: list[list[str]] = []
        self.cwds: list[str | None] = []
        self.frozen: list[SetupImage] = []
        self._handler = handler or (lambda command: _ok())

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        del timeout, env
        actual = (
            list(command) if not isinstance(command, str) else ["sh", "-c", command]
        )
        self.commands.append(actual)
        self.cwds.append(cwd)
        return self._handler(actual)

    def freeze(self) -> SetupImage:
        image = SetupImage(f"fake-setup-{len(self.frozen)}", self.run_id)
        self.frozen.append(image)
        return image


class FakeModel:
    def __init__(
        self,
        results: Sequence[ModelResult],
        *,
        budget: Budget | None = None,
    ) -> None:
        self.results = list(results)
        self.budget = budget or Budget(cap_usd=Decimal(1))
        self.requests: list[list[dict[str, object]]] = []

    def complete(
        self,
        messages: Sequence[dict[str, object]],
        *,
        max_tokens: int,
        deadline: Deadline,
    ) -> ModelResult:
        del max_tokens, deadline
        self.requests.append([dict(message) for message in messages])
        if not self.results:
            return ModelResult(Outcome.ERROR, error="no scripted reply")
        return self.results.pop(0)


def _action(tool: str, **arguments: object) -> ModelResult:
    return ModelResult(Outcome.OK, text=json.dumps({"tool": tool, **arguments}))


def _run(
    sandbox: FakeSandbox,
    actions: Sequence[ModelResult],
    *,
    budget: Budget | None = None,
) -> tuple[ReproduceResult, FakeModel]:
    actual_budget = budget or Budget(cap_usd=Decimal(1))
    model = FakeModel(actions, budget=actual_budget)
    result = reproduce_issue(
        cast(Sandbox, sandbox),
        actual_budget,
        issue_title=TITLE,
        issue_body=BODY,
        model=cast(ModelClient, model),
    )
    return result, model


def _file_handler(command: Sequence[str]) -> CommandResult:
    tool = {"list": "list_files", "read": "read_file", "write": "write_test"}[
        command[3]
    ]
    path = command[4]
    return _ok(f"{tool} {path}\n")


def test_every_tool_call_and_output_is_logged() -> None:
    sandbox = FakeSandbox(_file_handler)
    result, _ = _run(
        sandbox,
        [
            _action("list_files", path="."),
            _action("read_file", path="tests/test_existing.py"),
            _action(
                "write_test", path="tests/test_repro.py", content="def test_x(): pass"
            ),
            _action("done"),
        ],
    )

    assert result.outcome == OUTCOME_HANDED_TO_VERIFICATION
    assert [step.kind for step in result.steps] == [
        "list_files",
        "read_file",
        "write_test",
        "model",
    ]
    assert result.steps[0].stdout == "list_files .\n"
    assert result.steps[1].stdout == "read_file tests/test_existing.py\n"
    assert result.steps[2].stdout == "write_test tests/test_repro.py\n"
    assert result.test is not None
    assert result.test.path == "tests/test_repro.py"
    assert result.test.content == "def test_x(): pass"
    assert result.test.command == (
        "/tmp/venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "tests/test_repro.py",
    )
    log = result.log()
    assert len(cast(list[dict[str, object]], log["steps"])) == 4
    assert cast(dict[str, object], log["test"])["path"] == "tests/test_repro.py"


def test_model_gets_only_the_issue_title_and_body() -> None:
    sandbox = FakeSandbox()
    result, model = _run(sandbox, [_action("give_up")])

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    first = model.requests[0]
    content = "\n".join(str(message["content"]) for message in first)
    assert TITLE in content
    assert BODY in content
    assert "github.com" not in content
    assert set(first[0]) == {"role", "content"}
    assert set(first[1]) == {"role", "content"}


def test_model_done_hands_test_to_verification() -> None:
    sandbox = FakeSandbox(_file_handler)
    result, _ = _run(
        sandbox,
        [
            _action(
                "write_test", path="tests/test_repro.py", content="def test_x(): pass"
            ),
            _action("done"),
        ],
    )

    assert result.handed_to_verification
    assert result.reason is None
    assert result.image is not None
    assert result.image.ref == "fake-setup-0"
    assert result.test is not None
    assert result.test.path == "tests/test_repro.py"


def test_model_give_up_is_could_not_reproduce() -> None:
    sandbox = FakeSandbox()
    result, _ = _run(sandbox, [_action("give_up")])

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "could-not-reproduce"
    assert result.test is None
    assert result.steps[-1].kind == "model"


def test_cost_cap_stops_the_loop() -> None:
    sandbox = FakeSandbox()
    result, _ = _run(
        sandbox,
        [ModelResult(Outcome.STOPPED_AT_COST_CAP, error="the cap was reached")],
    )

    assert result.outcome == OUTCOME_STOPPED_AT_COST_CAP
    assert result.reason == "stopped-at-cost-cap"
    assert result.error == "the cap was reached"


def test_time_limit_stops_the_loop() -> None:
    sandbox = FakeSandbox()
    sandbox.deadline = Deadline(0)
    result, model = _run(sandbox, [_action("give_up")])

    assert result.outcome == OUTCOME_STOPPED_AT_TIME_LIMIT
    assert result.reason == "stopped-at-time-limit"
    assert model.requests == []


def test_model_error_after_retries_is_error() -> None:
    sandbox = FakeSandbox()
    result, _ = _run(
        sandbox,
        [ModelResult(Outcome.ERROR, error="the provider failed")],
    )

    assert result.outcome == OUTCOME_ERROR
    assert result.reason == "model-error"
    assert result.error == "the provider failed"


def test_tool_failure_output_is_returned_to_the_model_and_logged() -> None:
    def handler(command: Sequence[str]) -> CommandResult:
        return _failed("the file could not be read")

    sandbox = FakeSandbox(handler)
    result, model = _run(
        sandbox,
        [
            _action("read_file", path="tests/test_existing.py"),
            _action("give_up"),
        ],
    )

    assert result.steps[0].kind == "read_file"
    assert result.steps[0].stderr == "the file could not be read"
    second = model.requests[1]
    assert "the file could not be read" in str(second[-1]["content"])


def test_reproduce_refuses_a_model_with_a_different_budget() -> None:
    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel([], budget=Budget(cap_usd=Decimal(2)))
    with pytest.raises(ValueError, match="budget"):
        reproduce_issue(
            cast(Sandbox, FakeSandbox()),
            budget,
            issue_title=TITLE,
            issue_body=BODY,
            model=cast(ModelClient, model),
        )


@pytest.fixture(scope="session")
def test_image() -> str:
    subprocess.run(["docker", "pull", PROXY_IMAGE], check=True, capture_output=True)
    return PROXY_IMAGE


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text("def test_existing():\n    assert True\n")
    source = project / "src"
    source.mkdir()
    (source / "demo.py").write_text("def demo():\n    return 1\n")
    (project / "escape-file").symlink_to("/etc/passwd")
    (project / "escape-dir").symlink_to("/etc")
    (project / "linked-inside").symlink_to("tests")
    return project


@contextmanager
def _sandbox(tmp_path: Path, image: str) -> Iterator[Sandbox]:
    run_id = new_run_id()
    deadline = Deadline(1800)
    sandbox = Sandbox.start(image, run_id=run_id, deadline=deadline, role="setup")
    sandbox.copy_in(_project(tmp_path), CHECKOUT)
    try:
        yield sandbox
    finally:
        cleanup(run_id)


@pytest.fixture(scope="module")
def fixture_sandbox(
    test_image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Sandbox]:
    root = tmp_path_factory.mktemp("reproduce")
    with _sandbox(root, test_image) as sandbox:
        yield sandbox


def _docker_run(sandbox: Sandbox, actions: Sequence[ModelResult]) -> ReproduceResult:
    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel(actions, budget=budget)
    return reproduce_issue(
        sandbox,
        budget,
        issue_title=TITLE,
        issue_body=BODY,
        model=cast(ModelClient, model),
    )


def _exists(sandbox: Sandbox, path: str) -> bool:
    result = sandbox.run(
        [
            "python",
            "-c",
            (
                "import pathlib, sys; "
                "print(1 if pathlib.Path(sys.argv[1]).exists() else 0)"
            ),
            path,
        ]
    )
    return result.stdout.strip() == "1"


@pytest.mark.docker
def test_file_tools_read_the_fixture_checkout(fixture_sandbox: Sandbox) -> None:
    result = _docker_run(
        fixture_sandbox,
        [
            _action("list_files", path="tests"),
            _action("read_file", path="tests/test_existing.py"),
            _action("give_up"),
        ],
    )

    listing = result.steps[0]
    assert listing.exit_code == 0
    assert "test_existing.py" in listing.stdout
    reading = result.steps[1]
    assert reading.exit_code == 0
    assert "def test_existing" in reading.stdout


@pytest.mark.docker
def test_list_files_refuses_a_path_outside_the_checkout(
    fixture_sandbox: Sandbox,
) -> None:
    result = _docker_run(
        fixture_sandbox,
        [_action("list_files", path="escape-dir"), _action("give_up")],
    )

    step = result.steps[0]
    assert step.kind == "list_files"
    assert step.exit_code == 2
    assert "outside the checkout root" in step.stdout


@pytest.mark.docker
def test_read_file_refuses_a_path_outside_the_checkout(
    fixture_sandbox: Sandbox,
) -> None:
    result = _docker_run(
        fixture_sandbox,
        [_action("read_file", path="escape-file"), _action("give_up")],
    )

    step = result.steps[0]
    assert step.kind == "read_file"
    assert step.exit_code == 2
    assert "outside the checkout root" in step.stdout
    assert "root:" not in step.stdout


@pytest.mark.docker
def test_write_test_refuses_an_absolute_path(fixture_sandbox: Sandbox) -> None:
    name = f"{CHECKOUT}/tests/test_absolute_{uuid.uuid4().hex[:8]}.py"
    result = _docker_run(
        fixture_sandbox,
        [
            _action("write_test", path=name, content="def test_x(): pass"),
            _action("give_up"),
        ],
    )

    step = result.steps[0]
    assert step.kind == "write_test"
    assert step.exit_code == 2
    assert "relative to the checkout root" in step.stdout
    assert not _exists(fixture_sandbox, name)


@pytest.mark.docker
def test_write_test_refuses_a_parent_path(fixture_sandbox: Sandbox) -> None:
    name = f"tests/../tests/test_parent_{uuid.uuid4().hex[:8]}.py"
    result = _docker_run(
        fixture_sandbox,
        [
            _action("write_test", path=name, content="def test_x(): pass"),
            _action("give_up"),
        ],
    )

    step = result.steps[0]
    assert step.exit_code == 2
    assert "parent component" in step.stdout
    assert not _exists(fixture_sandbox, f"{CHECKOUT}/{name}")


@pytest.mark.docker
def test_write_test_refuses_a_path_outside_existing_test_directories(
    fixture_sandbox: Sandbox,
) -> None:
    name = f"src/test_outside_{uuid.uuid4().hex[:8]}.py"
    result = _docker_run(
        fixture_sandbox,
        [
            _action("write_test", path=name, content="def test_x(): pass"),
            _action("give_up"),
        ],
    )

    step = result.steps[0]
    assert step.exit_code == 2
    assert "existing test directories" in step.stdout
    assert not _exists(fixture_sandbox, f"{CHECKOUT}/{name}")


@pytest.mark.docker
def test_write_test_refuses_an_existing_file(fixture_sandbox: Sandbox) -> None:
    result = _docker_run(
        fixture_sandbox,
        [
            _action(
                "write_test",
                path="tests/test_existing.py",
                content="def test_changed(): pass",
            ),
            _action("give_up"),
        ],
    )

    step = result.steps[0]
    assert step.exit_code == 2
    assert "already exists" in step.stdout
    text = fixture_sandbox.run(
        ["python", "-c", "print(open('/tmp/checkout/tests/test_existing.py').read())"]
    )
    assert "def test_existing" in text.stdout


@pytest.mark.docker
def test_write_test_refuses_a_symlinked_directory(fixture_sandbox: Sandbox) -> None:
    suffix = uuid.uuid4().hex[:8]
    name = f"linked-inside/test_link_{suffix}.py"
    result = _docker_run(
        fixture_sandbox,
        [
            _action("write_test", path=name, content="def test_x(): pass"),
            _action("give_up"),
        ],
    )

    step = result.steps[0]
    assert step.exit_code == 2
    assert "symlink" in step.stdout
    assert not _exists(fixture_sandbox, f"{CHECKOUT}/tests/test_link_{suffix}.py")


@pytest.mark.docker
def test_write_test_refuses_a_second_test_file(fixture_sandbox: Sandbox) -> None:
    suffix = uuid.uuid4().hex[:8]
    first = f"tests/test_first_{suffix}.py"
    second = f"tests/test_second_{suffix}.py"
    result = _docker_run(
        fixture_sandbox,
        [
            _action("write_test", path=first, content="def test_first(): pass"),
            _action("write_test", path=second, content="def test_second(): pass"),
            _action("give_up"),
        ],
    )

    assert result.steps[0].kind == "write_test"
    assert result.steps[0].exit_code == 0
    assert result.steps[1].exit_code == 2
    assert "already been written" in result.steps[1].stdout
    assert _exists(fixture_sandbox, f"{CHECKOUT}/{first}")
    assert not _exists(fixture_sandbox, f"{CHECKOUT}/{second}")


@pytest.mark.docker
def test_run_test_runs_once_in_a_new_no_network_container(
    test_image: str, tmp_path: Path
) -> None:
    marker = "MARKER-" + uuid.uuid4().hex
    shim = """#!/usr/bin/env python
import socket
import sys
from pathlib import Path

path = Path(sys.argv[-1])
print("TEST-CONTENT:" + path.read_text())
try:
    socket.create_connection(("example.com", 443), 2)
except OSError:
    print("NETWORK:blocked")
else:
    print("NETWORK:connected")
raise SystemExit(1)
"""
    run_id = new_run_id()
    deadline = Deadline(300)
    sandbox = Sandbox.start(test_image, run_id=run_id, deadline=deadline, role="setup")
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
                    f"p.write_text({shim!r}); "
                    "p.chmod(0o755)"
                ),
            ]
        )
        assert install.ok, install.stderr
        budget = Budget(cap_usd=Decimal(1))
        model = FakeModel(
            [
                _action(
                    "write_test",
                    path="tests/test_repro.py",
                    content=f"{marker}\n",
                ),
                _action("run_test"),
                _action("done"),
            ],
            budget=budget,
        )
        result = reproduce_issue(
            sandbox,
            budget,
            issue_title=TITLE,
            issue_body=BODY,
            model=cast(ModelClient, model),
        )
    finally:
        cleanup(run_id)

    assert result.outcome == OUTCOME_HANDED_TO_VERIFICATION
    assert result.image is not None
    runs = [step for step in result.steps if step.kind == "run_test"]
    assert len(runs) == 1
    assert runs[0].exit_code == 1
    assert f"TEST-CONTENT:{marker}" in runs[0].stdout
    assert "NETWORK:blocked" in runs[0].stdout
