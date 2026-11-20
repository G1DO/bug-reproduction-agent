from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from bugrepro.egress_proxy import FETCH_HOSTS, Phase
from bugrepro.languages import python as python_adapter
from bugrepro.languages.python import (
    CHECKOUT,
    OUTCOME_FAILED,
    OUTCOME_STOPPED_AT_COST_CAP,
    OUTCOME_STOPPED_AT_TIME_LIMIT,
    OUTCOME_SUCCEEDED,
    PYTHON_IMAGES,
    UV_BIN,
    AuditError,
    Distribution,
    InstalledPackage,
    ProjectInfo,
    SetupResult,
    SetupStep,
    UrlDependency,
    audit_installed,
    build_environment,
    collect_count,
    error_patterns,
    failure_error_text,
    failure_raising_file,
    inspect_project,
    latest_release_before,
    pytest_failure_counts,
    registries,
)
from bugrepro.languages.python import test_command as make_test_command
from bugrepro.model import Budget, ModelClient, ModelResult, Outcome
from bugrepro.sandbox import (
    FETCHER_IMAGE,
    PROXY_IMAGE,
    Checkout,
    CommandResult,
    Deadline,
    EgressProxy,
    Sandbox,
    SetupFailed,
    TimeLimitExceeded,
    cleanup,
    create_egress_network,
    create_network,
    fetch_checkout,
    new_run_id,
    run_isolated,
)

BASE = datetime(2020, 5, 20, 20, 51, 6, tzinfo=UTC)
BASE_TEXT = "2020-05-20T20:51:06Z"


class FakeSandbox:
    def __init__(
        self,
        handler: Callable[[Sequence[str], dict[str, str]], CommandResult] | None = None,
    ) -> None:
        self.deadline = Deadline(120)
        self.role = "setup"
        self.image = PYTHON_IMAGES["3.10"]
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.allowed: list[tuple[Phase, tuple[str, ...]]] = []
        self._handler = handler or self._default

    def allow(self, phase: Phase, hosts: Sequence[str]) -> None:
        self.allowed.append((phase, tuple(sorted(hosts))))

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        actual = (
            list(command) if not isinstance(command, str) else ["sh", "-c", command]
        )
        self.commands.append(actual)
        self.environments.append(dict(env or {}))
        return self._handler(actual, env or {})

    def _default(
        self, command: Sequence[str], environment: dict[str, str]
    ) -> CommandResult:
        if primary(command) == "python" and "-m" in command:
            if "venv" in command:
                return ok("")
            return ok("")
        if primary(command) == "uv":
            return ok("")
        if primary(command).endswith("python") and "-c" in command:
            return ok(_installed_rows())
        if "-m" in command and "pytest" in command:
            if "--collect-only" in command:
                return ok("1 test collected in 0.01s\n")
            return ok("1 passed in 0.01s\n")
        return ok("")


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
        self.requests.append(list(messages))
        if not self.results:
            return ModelResult(Outcome.ERROR, error="no scripted reply")
        return self.results.pop(0)


def primary(command: Sequence[str]) -> str:
    return command[0] if command else ""


def ok(stdout: str) -> CommandResult:
    return CommandResult(0, stdout, "", 0.01)


def failed(stderr: str) -> CommandResult:
    return CommandResult(1, "", stderr, 0.01)


def _installed_rows(name: str = "demo") -> str:
    direct = json.dumps({"dir_info": {}})
    return json.dumps(
        {
            "rows": [
                {"name": name, "version": "0.1.0", "direct_url": direct},
                {"name": "localdep", "version": "1.0", "direct_url": direct},
            ]
        }
    )


def _project(**changes: object) -> ProjectInfo:
    values: dict[str, object] = {
        "python_version": "3.8",
        "image": PYTHON_IMAGES["3.8"],
        "requires_python": ">=3.8",
        "project_name": "demo",
        "project_version": "0.1.0",
        "uses_scm": False,
        "extras": (),
        "extra_dependencies": {},
        "django_settings": None,
        "django_suite": False,
        "matplotlib_build": False,
        "importlib_mode": False,
        "git_dependencies": (),
        "url_dependencies": (),
        "sources": ("pyproject.toml",),
    }
    values.update(changes)
    return ProjectInfo(**values)  # type: ignore[arg-type]


def _inspect_payload(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "python_versions": ["3.8", "3.9"],
        "pinned_python": None,
        "requires_python": ">=3.8",
        "project_name": "demo",
        "project_version": "0.1.0",
        "uses_scm": False,
        "extras": ["test"],
        "extra_dependencies": {},
        "git_dependencies": [],
        "url_dependencies": [],
        "sources": ["pyproject.toml"],
    }
    values.update(changes)
    return values


def _checkout(payload: dict[str, object]) -> Checkout:
    fetcher = FakeSandbox(
        lambda command, environment: ok(json.dumps(payload)),
    )
    fetcher.role = "fetch"
    return Checkout(
        fetcher=cast(Sandbox, fetcher),
        synthetic_commit="a" * 40,
        tree="b" * 40,
        base_sha="c" * 40,
    )


def test_python_images_are_digest_pinned() -> None:
    assert set(PYTHON_IMAGES) == {"3.8", "3.9", "3.10"}
    for image in PYTHON_IMAGES.values():
        assert re.fullmatch(r"python:\d+\.\d+@sha256:[0-9a-f]{64}", image)


def test_inspect_project_prefers_the_highest_supported_metadata_version() -> None:
    project = inspect_project(
        _checkout(_inspect_payload()),
        base_time=datetime(2021, 6, 1, tzinfo=UTC),
    )
    assert project.python_version == "3.9"
    assert project.image == PYTHON_IMAGES["3.9"]
    assert project.extras == ("test",)
    assert project.inspection is not None
    assert project.inspection.kind == "inspect-project"


def test_inspect_project_ignores_a_python_released_after_the_base_time() -> None:
    project = inspect_project(
        _checkout(_inspect_payload(python_versions=["3.9", "3.10"])),
        base_time=datetime(2021, 6, 20, tzinfo=UTC),
    )
    assert project.python_version == "3.9"


def test_inspect_project_uses_a_pinned_python_version() -> None:
    project = inspect_project(
        _checkout(_inspect_payload(pinned_python="3.8", python_versions=["3.9"])),
        base_time=BASE,
    )
    assert project.python_version == "3.8"


def test_inspect_project_refuses_an_unsupported_python() -> None:
    with pytest.raises(SetupFailed) as error:
        inspect_project(
            _checkout(_inspect_payload(requires_python=">=3.11")),
            base_time=BASE,
        )
    assert error.value.reason == "unsupported-python"


def test_inspect_project_refuses_malformed_metadata() -> None:
    with pytest.raises(SetupFailed) as error:
        inspect_project(
            _checkout(_inspect_payload(python_versions="3.9")),
            base_time=BASE,
        )
    assert error.value.reason == "metadata-invalid"


def test_inspect_project_refuses_an_extra_dependency_option() -> None:
    with pytest.raises(SetupFailed) as error:
        inspect_project(
            _checkout(
                _inspect_payload(
                    extra_dependencies={"test": ["--find-links=/tmp/checkout"]}
                )
            ),
            base_time=BASE,
        )
    assert error.value.reason == "metadata-invalid"


def test_registries_are_only_the_python_package_hosts() -> None:
    assert registries() == frozenset({"pypi.org", "files.pythonhosted.org"})
    assert "github.com" not in registries()


def test_test_command_uses_the_venv_pytest() -> None:
    assert make_test_command("tests/test_one.py") == [
        "/tmp/venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "--tb=long",
        "--color=no",
        "tests/test_one.py",
    ]


def test_collect_count_parses_pytest_output() -> None:
    assert collect_count("2717 tests collected in 0.50s") == 2717
    assert collect_count("collected 9422 items") == 9422
    assert collect_count("tests/test_a.py::test_one\ntests/test_a.py::test_two\n") == 2
    assert collect_count("COUNT=14479\n") == 14479
    with pytest.raises(ValueError):
        collect_count("collection error")


def test_error_patterns_extract_exception_names_and_the_last_line() -> None:
    report = "The failing test raises:\n```\nValueError: bad value\n```\n"
    patterns = error_patterns(report)
    assert "ValueError" in patterns
    assert "ValueError: bad value" in patterns


def test_error_patterns_ignores_prose_without_an_error() -> None:
    assert error_patterns("The parser drops the last line and exits early.") == ()


def test_error_patterns_reads_an_unfenced_traceback() -> None:
    report = (
        "Traceback (most recent call last):\n"
        '  File "parser.py", line 9, in parse\n'
        "ValueError: bad value\n"
    )
    patterns = error_patterns(report)
    assert "ValueError" in patterns
    assert "ValueError: bad value" in patterns


def test_error_patterns_reads_an_inline_error_line() -> None:
    assert "ValueError: bad value" in error_patterns(
        "It fails with ValueError: bad value"
    )


def test_error_patterns_reads_a_full_pytest_log() -> None:
    report = (
        "The failure:\n"
        "```\n"
        "FAILED tests/test_repro.py::test_repro - ValueError: bad value\n"
        "1 failed in 0.03s\n"
        "```\n"
    )
    patterns = error_patterns(report)
    assert "ValueError" in patterns
    assert "ValueError: bad value" in patterns


def test_failure_raising_file_returns_the_last_traceback_frame() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "tests/test_repro.py:2: in test_repro\n"
        "    parser.parse('x')\n"
        "src/parser.py:9: in parse\n"
        "    raise ValueError('bad value')\n"
        "E   ValueError: bad value\n"
    )
    assert failure_raising_file(output, "ValueError") == "src/parser.py"


def test_failure_raising_file_reads_pytest_long_form() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "    def test_repro():\n"
        ">       parser.parse('x')\n"
        "E       ValueError: bad value\n"
        "\n"
        "tests/parser.py:2: ValueError\n"
        "1 failed in 0.03s\n"
    )
    assert failure_raising_file(output, "ValueError") == "tests/parser.py"


def test_failure_raising_file_reads_a_test_raise_in_long_form() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "    def test_repro():\n"
        ">       raise ValueError('boom')\n"
        "E       ValueError: boom\n"
        "\n"
        "tests/test_repro.py:2: ValueError\n"
        "1 failed in 0.03s\n"
    )
    assert failure_raising_file(output, "ValueError") == "tests/test_repro.py"


def test_failure_error_text_ignores_assertion_diff_lines() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "    def test_repro():\n"
        ">       assert value == ''\n"
        "E       AssertionError: assert 'x' == ''\n"
        "E         + tests/other.py:2: ValueError\n"
        "tests/test_repro.py:2: AssertionError\n"
        "1 failed in 0.03s\n"
    )
    text = failure_error_text(output)
    assert "AssertionError" in text
    assert "ValueError" not in text


def test_failure_error_text_ignores_final_like_source_lines() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "    def test_repro():\n"
        "        assert (\n"
        "        tests/indented.py:2: ValueError\n"
        "tests/other.py:2: ValueError\n"
        "        ) == ''\n"
        "E       AssertionError: assert '' == ''\n"
        "tests/test_repro.py:2: AssertionError\n"
        "1 failed in 0.03s\n"
    )
    text = failure_error_text(output)
    assert "AssertionError" in text
    assert "ValueError" not in text


def test_failure_error_text_ignores_output_after_the_footer() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "tests/test_repro.py:2: in test_repro\n"
        "    raise ValueError('boom')\n"
        "E   ValueError: boom\n"
        "tests/test_repro.py:2: ValueError\n"
        "1 failed in 0.03s\n"
        "FAILED tests/test_fake.py::test_fake - ValueError: bad value\n"
    )
    text = failure_error_text(output)
    assert "ValueError: boom" in text
    assert "ValueError: bad value" not in text


def test_failure_error_text_ignores_a_forged_section_after_a_passing_footer() -> None:
    output = (
        "============================= test session starts ==============================\n"
        "tests/test_repro.py .                                                      [100%]\n"
        "1 passed in 0.02s\n"
        "=================================== FAILURES ===================================\n"
        "E   ValueError: bad value\n"
        "tests/parser.py:2: ValueError\n"
        "1 failed in 0.01s\n"
    )
    assert failure_error_text(output) == ""


def test_failure_error_text_ignores_the_short_summary_message() -> None:
    output = (
        "============================= test session starts ==============================\n"
        "=================================== FAILURES ===================================\n"
        "tests/test_repro.py:2: in test_repro\n"
        "    parser.divide()\n"
        "E   ZeroDivisionError: division by zero\n"
        "tests/parser.py:2: ZeroDivisionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_repro.py::test_repro - ValueError: bad value\n"
        "1 failed in 0.03s\n"
    )
    text = failure_error_text(output)
    assert "ZeroDivisionError" in text
    assert "ValueError" not in text


def test_pytest_failure_counts_reads_the_first_footer() -> None:
    captured = (
        "----------------------------- Captured stdout call -----------------------------\n"
        "1 failed in 0.01s\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_a\n"
        "FAILED tests/test_b.py::test_b\n"
        "2 failed in 0.03s\n"
    )
    passing = "1 passed in 0.02s\n1 failed in 0.01s\n"
    assert pytest_failure_counts(captured) == (2, 0, 0, 0)
    assert pytest_failure_counts(passing) == (0, 0, 1, 0)


def test_failure_raising_file_ignores_frames_in_captured_output() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "tests/test_repro.py:2: in test_repro\n"
        "    raise ValueError('boom')\n"
        "E   ValueError: boom\n"
        "----------------------------- Captured stdout call -----------------------------\n"
        "src/other.py:9: in parse\n"
        "1 failed in 0.03s\n"
    )
    assert failure_raising_file(output, "ValueError") == "tests/test_repro.py"


def test_failure_error_text_excludes_paths_source_and_captured_output() -> None:
    output = (
        "=================================== FAILURES ===================================\n"
        "___________________________ test_ValueError ____________________________\n"
        "    def test_ValueError():\n"
        ">       print('ValueError: bad value')\n"
        "E       assert False\n"
        "\n"
        "tests/test_ValueError.py:2: AssertionError\n"
        "----------------------------- Captured stdout call -----------------------------\n"
        "ValueError: bad value\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_ValueError.py::test_ValueError - assert False\n"
        "1 failed in 0.03s\n"
    )
    text = failure_error_text(output)
    assert "assert False" in text
    assert "AssertionError" in text
    assert "tests/test_ValueError.py" not in text
    assert "ValueError: bad value" not in text


def test_inspect_script_reads_setup_py_extras(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(extras_require={\n"
        "    'testing': ['pytest', 'html5lib'],\n"
        "})\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", python_adapter._INSPECT_SCRIPT, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["extras"] == ["testing"]
    assert payload["extra_dependencies"] == {"testing": ["pytest", "html5lib"]}


def test_result_line_records_newer_versions() -> None:
    result = SetupResult(
        outcome=OUTCOME_SUCCEEDED,
        reason=None,
        image=PYTHON_IMAGES["3.10"],
        python_version="3.10",
        base_time=BASE_TEXT,
        steps=(),
        installed=(
            InstalledPackage(
                name="demo",
                version="1.0",
                upload_time="2020-05-21T00:00:00Z",
                source="pypi",
                newer=True,
            ),
        ),
    )
    assert result.log()["newer"] == [
        {
            "name": "demo",
            "version": "1.0",
            "upload_time": "2020-05-21T00:00:00Z",
            "source": "pypi",
            "host": None,
            "newer": True,
            "unbounded": False,
        }
    ]


def test_setup_succeeds_and_logs_every_step() -> None:
    sandbox = FakeSandbox()
    result = build_environment(
        cast(Sandbox, sandbox),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(),
    )

    assert result.outcome == OUTCOME_SUCCEEDED
    assert result.collect_count == 1
    assert sandbox.allowed == [(Phase.INSTALL, tuple(sorted(registries())))]
    install = next(
        command
        for command in sandbox.commands
        if command[0] == UV_BIN and "install" in command
    )
    assert install[install.index("--exclude-newer") + 1] == BASE_TEXT
    assert "--reinstall" in install
    log = result.log()
    assert log["dependency_bound"] == {
        "ecosystem": "pypi",
        "kind": "exclude-newer",
        "time": BASE_TEXT,
        "reinstall": True,
    }
    for step in cast(list[dict[str, object]], log["steps"]):
        assert set(step) == {
            "kind",
            "command",
            "stdout",
            "stderr",
            "exit_code",
            "duration_s",
            "timed_out",
            "stdout_truncated",
            "stderr_truncated",
        }


def test_inspection_step_is_part_of_the_setup_log() -> None:
    inspection = SetupStep(
        kind="inspect-project",
        command=("python", "-c", "inspect", CHECKOUT),
        stdout="{}",
        stderr="",
        exit_code=0,
        duration_s=0.1,
    )
    result = build_environment(
        cast(Sandbox, FakeSandbox()),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(inspection=inspection),
    )
    assert result.steps[0] is inspection
    steps = cast(list[dict[str, object]], result.log()["steps"])
    assert steps[0]["kind"] == "inspect-project"


def test_matplotlib_build_uses_the_pinned_qhull_tool() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if primary(command).endswith("python") and "-c" in command:
            return ok(_installed_rows("matplotlib"))
        return FakeSandbox()._default(command, environment)

    sandbox = FakeSandbox(handler)
    result = build_environment(
        cast(Sandbox, sandbox),
        Budget(cap_usd=Decimal(1)),
        base_time=datetime(2022, 11, 30, 21, 1, 49, tzinfo=UTC),
        project=_project(
            project_name="matplotlib",
            python_version="3.10",
            image=PYTHON_IMAGES["3.10"],
            requires_python=">=3.10",
            matplotlib_build=True,
            importlib_mode=True,
        ),
    )
    assert result.outcome == OUTCOME_SUCCEEDED
    tool = next(
        command
        for command in sandbox.commands
        if any("cmeel-qhull" in argument for argument in command)
    )
    assert "cmeel-qhull==8.0.2.1" in tool
    assert any(
        environment.get("CPATH", "").endswith("cmeel.prefix/include")
        for environment in sandbox.environments
    )
    install = next(
        command
        for command in sandbox.commands
        if command[0] == UV_BIN and "install" in command and "--editable" in command
    )
    assert CHECKOUT in install


def test_django_suite_does_not_install_pytest() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if primary(command).endswith("python") and "-c" in command:
            script = command[2] if len(command) > 2 else ""
            if "COUNT" in script:
                return ok("COUNT=3\n")
            return ok(_installed_rows("django"))
        return FakeSandbox()._default(command, environment)

    sandbox = FakeSandbox(handler)
    result = build_environment(
        cast(Sandbox, sandbox),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(
            project_name="django",
            django_settings="tests.test_sqlite",
            django_suite=True,
        ),
    )
    assert result.outcome == OUTCOME_SUCCEEDED
    assert result.collect_count == 3
    assert not any(
        "pytest" in command or "pytest-django" in command
        for command in sandbox.commands
    )


def test_naive_base_time_is_refused() -> None:
    with pytest.raises(ValueError):
        build_environment(
            cast(Sandbox, FakeSandbox()),
            Budget(cap_usd=Decimal(1)),
            base_time=datetime(2020, 5, 20, 20, 51, 6),
            project=_project(),
        )


def test_setup_refuses_an_extra_dependency_option() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if f"{CHECKOUT}[test]" in command:
            return failed("the extra could not be installed")
        return FakeSandbox()._default(command, environment)

    sandbox = FakeSandbox(handler)
    result = build_environment(
        cast(Sandbox, sandbox),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(
            extras=("test",),
            extra_dependencies={"test": ("--find-links=/tmp/checkout",)},
        ),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "metadata-invalid"
    assert not any(
        "--find-links" in argument
        for command in sandbox.commands
        for argument in command
    )


def test_setup_refuses_python_released_after_the_base_time() -> None:
    result = build_environment(
        cast(Sandbox, FakeSandbox()),
        Budget(cap_usd=Decimal(1)),
        base_time=datetime(2019, 1, 1, tzinfo=UTC),
        project=_project(),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "unsupported-python"


def test_git_dependency_is_setup_failed() -> None:
    result = build_environment(
        cast(Sandbox, FakeSandbox()),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(
            git_dependencies=("demo @ git+https://github.com/example/demo.git",)
        ),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "git-dependency"
    assert "git+" in (result.error or "")


def test_dynamic_git_dependency_is_setup_failed() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if primary(command) == UV_BIN and "install" in command:
            return failed("error: failed to fetch https://github.com/example/repo.git")
        return FakeSandbox()._default(command, environment)

    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "git-dependency"


def test_url_dependency_on_an_allowlisted_host_is_unbounded() -> None:
    result = build_environment(
        cast(Sandbox, FakeSandbox()),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(
            url_dependencies=(
                UrlDependency(
                    requirement="demo",
                    url="https://files.pythonhosted.org/packages/demo.whl",
                    host="files.pythonhosted.org",
                ),
            )
        ),
    )
    assert result.outcome == OUTCOME_SUCCEEDED
    assert result.log()["unbounded"] == [
        {
            "kind": "url",
            "requirement": "demo",
            "host": "files.pythonhosted.org",
        }
    ]


def test_resolver_failure_keeps_the_commands_and_output() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if primary(command) == UV_BIN and "install" in command:
            return failed("no version satisfies the date bound")
        return FakeSandbox()._default(command, environment)

    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "resolver-failed"
    assert "resolver" in (result.error or "")
    failed_steps = [
        step
        for step in result.steps
        if step.exit_code == 1 and "install" in step.command
    ]
    assert failed_steps
    assert "no version satisfies" in failed_steps[0].stderr


def test_setup_stops_at_the_time_limit() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if primary(command) == "python" and "venv" in command:
            raise TimeLimitExceeded("the run's time limit has been reached")
        return ok("")

    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(),
    )
    assert result.outcome == OUTCOME_STOPPED_AT_TIME_LIMIT
    assert result.reason == "stopped-at-time-limit"


def test_setup_stops_at_the_cost_cap() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if "-m" in command and "pytest" in command and "--collect-only" in command:
            return failed("collection error")
        return FakeSandbox()._default(command, environment)

    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel([ModelResult(Outcome.STOPPED_AT_COST_CAP)], budget=budget)
    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        budget,
        base_time=BASE,
        project=_project(),
        model=cast(ModelClient, model),
    )
    assert result.outcome == OUTCOME_STOPPED_AT_COST_CAP
    assert result.reason == "stopped-at-cost-cap"


def test_setup_refuses_a_model_with_a_different_budget() -> None:
    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel([], budget=Budget(cap_usd=Decimal(2)))
    with pytest.raises(ValueError, match="budget"):
        build_environment(
            cast(Sandbox, FakeSandbox()),
            budget,
            base_time=BASE,
            project=_project(),
            model=cast(ModelClient, model),
        )


def test_model_command_cannot_change_the_date_bound() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if "-m" in command and "pytest" in command and "--collect-only" in command:
            return failed("collection error")
        return FakeSandbox()._default(command, environment)

    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel(
        [
            ModelResult(
                Outcome.OK,
                text=json.dumps(
                    {
                        "commands": [
                            [
                                "uv",
                                "pip",
                                "install",
                                "--exclude-newer",
                                "1999-01-01T00:00:00Z",
                                "requests",
                            ]
                        ]
                    }
                ),
            )
        ],
        budget=budget,
    )
    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        budget,
        base_time=BASE,
        project=_project(),
        model=cast(ModelClient, model),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "model-command-invalid"


def test_model_python_c_command_is_refused() -> None:
    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if "-m" in command and "pytest" in command and "--collect-only" in command:
            return failed("collection error")
        return FakeSandbox()._default(command, environment)

    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel(
        [
            ModelResult(
                Outcome.OK,
                text=json.dumps(
                    {
                        "commands": [
                            [
                                "python",
                                "-c",
                                (
                                    "import urllib.request; "
                                    "urllib.request.urlopen('https://pypi.org')"
                                ),
                            ]
                        ]
                    }
                ),
            )
        ],
        budget=budget,
    )
    result = build_environment(
        cast(Sandbox, FakeSandbox(handler)),
        budget,
        base_time=BASE,
        project=_project(),
        model=cast(ModelClient, model),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "model-command-invalid"


def test_model_command_gets_the_exact_date_bound() -> None:
    collections = {"count": 0}

    def handler(command: Sequence[str], environment: dict[str, str]) -> CommandResult:
        if "-m" in command and "pytest" in command and "--collect-only" in command:
            collections["count"] += 1
            if collections["count"] == 1:
                return failed("collection error")
            return ok("1 test collected in 0.01s\n")
        return FakeSandbox()._default(command, environment)

    sandbox = FakeSandbox(handler)
    budget = Budget(cap_usd=Decimal(1))
    model = FakeModel(
        [
            ModelResult(
                Outcome.OK,
                text=json.dumps({"commands": [["uv", "pip", "install", "requests"]]}),
            )
        ],
        budget=budget,
    )
    result = build_environment(
        cast(Sandbox, sandbox),
        budget,
        base_time=BASE,
        project=_project(),
        model=cast(ModelClient, model),
    )
    assert result.outcome == OUTCOME_SUCCEEDED
    model_command = next(
        command
        for command in sandbox.commands
        if command[0] == "uv" and "requests" in command
    )
    assert model_command[model_command.index("--exclude-newer") + 1] == BASE_TEXT
    assert "--reinstall" in model_command


class _Registry:
    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                registry.headers.append(dict(self.headers.items()))
                if self.path == "/pypi/demo/1.0/json":
                    self._send(
                        {"urls": [{"upload_time_iso_8601": "2020-05-21T00:00:00Z"}]}
                    )
                elif self.path == "/pypi/demo/0.9/json":
                    self._send(
                        {"urls": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}]}
                    )
                elif self.path == "/pypi/demo/json":
                    self._send(
                        {
                            "releases": {
                                "0.9": [
                                    {"upload_time_iso_8601": ("2020-01-01T00:00:00Z")}
                                ],
                                "1.0": [
                                    {"upload_time_iso_8601": ("2020-05-21T00:00:00Z")}
                                ],
                            }
                        }
                    )
                else:
                    self.send_error(404)

            def _send(self, payload: dict[str, object]) -> None:
                encoded = json.dumps(payload).encode()
                self.send_response(200)
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
        return f"http://127.0.0.1:{int(self.server.server_address[1])}/pypi"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


@contextmanager
def _registry() -> Iterator[_Registry]:
    registry = _Registry()
    try:
        yield registry
    finally:
        registry.close()


def test_audit_flags_a_version_uploaded_after_the_base_time() -> None:
    with _registry() as registry:
        packages = audit_installed(
            [
                Distribution(name="demo", version="1.0"),
                Distribution(name="demo", version="0.9"),
            ],
            BASE,
            deadline=Deadline(30),
            registry_url=registry.url,
        )
    assert packages[0].newer is True
    assert packages[0].upload_time == "2020-05-21T00:00:00Z"
    assert packages[1].newer is False
    assert packages[1].upload_time == "2020-01-01T00:00:00Z"
    assert registry.headers
    assert all("Authorization" not in headers for headers in registry.headers)


def test_audit_marks_a_url_dependency_unbounded() -> None:
    packages = audit_installed(
        [
            Distribution(
                name="demo",
                version="1.0",
                direct_url={
                    "url": "https://files.pythonhosted.org/packages/demo.whl",
                    "archive_info": {},
                },
            )
        ],
        BASE,
    )
    assert packages[0].unbounded is True
    assert packages[0].source == "url"
    assert packages[0].host == "files.pythonhosted.org"


def test_audit_sends_no_token() -> None:
    with _registry() as registry:
        audit_installed(
            [Distribution(name="demo", version="1.0")],
            BASE,
            deadline=Deadline(30),
            registry_url=registry.url,
        )
    assert registry.headers
    assert all("Authorization" not in headers for headers in registry.headers)


def test_audit_missing_version_is_a_setup_failure() -> None:
    with _registry() as registry, pytest.raises(AuditError):
        audit_installed(
            [Distribution(name="missing", version="1.0")],
            BASE,
            deadline=Deadline(30),
            registry_url=registry.url,
        )


def test_audit_failure_is_setup_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise AuditError("no upload time")

    monkeypatch.setattr(python_adapter, "audit_installed", fail)
    result = build_environment(
        cast(Sandbox, FakeSandbox()),
        Budget(cap_usd=Decimal(1)),
        base_time=BASE,
        project=_project(),
    )
    assert result.outcome == OUTCOME_FAILED
    assert result.reason == "audit-failed"
    assert result.error == "no upload time"
    assert any(step.kind == "list-installed" for step in result.steps)


def test_latest_release_before_the_base_time() -> None:
    with _registry() as registry:
        version = latest_release_before(
            "demo",
            BASE,
            deadline=Deadline(30),
            registry_url=registry.url,
        )
    assert version == "0.9"


@pytest.mark.docker
def test_python_setup_collects_a_fixture_projects_tests(tmp_path: Path) -> None:
    image = PYTHON_IMAGES["3.10"]
    subprocess.run(["docker", "pull", image], check=True, capture_output=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "bugrepro-fixture"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["pytest"]
"""
    )
    tests = project_dir / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    run_id = new_run_id()
    deadline = Deadline(600)
    proxy: EgressProxy | None = None
    setup: Sandbox | None = None
    try:
        internal = create_network(run_id)
        egress_network = create_egress_network(run_id)
        proxy = EgressProxy.start(
            run_id=run_id,
            deadline=deadline,
            internal_network=internal,
            egress_network=egress_network,
            phase=Phase.INSTALL,
            hosts=registries(),
        )
        setup = Sandbox.start(
            image,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
            role="setup",
        )
        setup.copy_in(project_dir, CHECKOUT)
        identity = setup.run(["id", "-u"])
        assert identity.stdout.strip() == "1000"

        project = ProjectInfo(
            python_version="3.10",
            image=image,
            requires_python=">=3.10",
            project_name="bugrepro-fixture",
            project_version="0.1.0",
            uses_scm=False,
            extras=(),
            extra_dependencies={},
            django_settings=None,
            django_suite=False,
            matplotlib_build=False,
            importlib_mode=False,
            git_dependencies=(),
            url_dependencies=(),
            sources=("pyproject.toml",),
        )
        result = build_environment(
            setup,
            Budget(cap_usd=Decimal(1)),
            base_time=datetime(2024, 6, 1, tzinfo=UTC),
            project=project,
        )
        assert result.outcome == OUTCOME_SUCCEEDED, result.log()
        assert result.collect_count == 1

        frozen = setup.freeze()
        setup = None
        run = run_isolated(
            frozen,
            make_test_command("tests/test_ok.py"),
            timeout=60,
            run_id=run_id,
            deadline=Deadline(60),
            workdir=CHECKOUT,
        )
        assert run.ok, (run.stdout, run.stderr)
        assert "1 passed" in run.stdout
    finally:
        if setup is not None:
            setup.kill()
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)


SPIKE_ISSUES = (
    pytest.param(
        "pytest-dev__pytest-7236",
        "https://github.com/pytest-dev/pytest.git",
        "c98bc4cd3d687fe9b392d8eecd905627191d4f06",
        datetime(2020, 5, 20, 20, 51, 6, tzinfo=UTC),
        2832,
        id="pytest-7236",
    ),
    pytest.param(
        "sympy__sympy-18189",
        "https://github.com/sympy/sympy.git",
        "1923822ddf8265199dbd9ef9ce09641d3fd042b9",
        datetime(2019, 12, 31, 15, 17, 38, tzinfo=UTC),
        9422,
        id="sympy-18189",
    ),
    pytest.param(
        "sphinx-doc__sphinx-9367",
        "https://github.com/sphinx-doc/sphinx.git",
        "6918e69600810a4664e53653d6ff0290c3c4a788",
        datetime(2021, 6, 20, 14, 58, 7, tzinfo=UTC),
        1718,
        id="sphinx-9367",
    ),
    pytest.param(
        "django__django-14017",
        "https://github.com/django/django.git",
        "466920f6d726eee90d5566e0a9948e92b33a122e",
        datetime(2021, 2, 18, 20, 13, 24, tzinfo=UTC),
        14479,
        id="django-14017",
    ),
    pytest.param(
        "matplotlib__matplotlib-24570",
        "https://github.com/matplotlib/matplotlib.git",
        "8f0003ae902952372824c9917975fb372c026a42",
        datetime(2022, 11, 30, 21, 1, 49, tzinfo=UTC),
        9201,
        id="matplotlib-24570",
    ),
)


@pytest.mark.docker
@pytest.mark.parametrize(
    ("instance_id", "repo", "sha", "base_time", "expected_count"), SPIKE_ISSUES
)
def test_spike18_issues_setup_as_predicted(
    instance_id: str,
    repo: str,
    sha: str,
    base_time: datetime,
    expected_count: int,
) -> None:
    if os.environ.get("BUGREPRO_RUN_SPIKE") != "1":
        pytest.skip("set BUGREPRO_RUN_SPIKE=1 to run the five #18 issues")
    for image in (PROXY_IMAGE, FETCHER_IMAGE):
        subprocess.run(["docker", "pull", image], check=True, capture_output=True)
    run_id = new_run_id()
    deadline = Deadline(1800)
    proxy: EgressProxy | None = None
    setup: Sandbox | None = None
    started = time.monotonic()
    try:
        internal = create_network(run_id)
        egress_network = create_egress_network(run_id)
        proxy = EgressProxy.start(
            run_id=run_id,
            deadline=deadline,
            internal_network=internal,
            egress_network=egress_network,
            phase=Phase.FETCH,
            hosts=FETCH_HOSTS,
        )
        with fetch_checkout(
            repo,
            sha,
            run_id=run_id,
            deadline=deadline,
            egress=proxy,
        ) as checkout:
            project_info = inspect_project(checkout, base_time=base_time)
            subprocess.run(
                ["docker", "pull", project_info.image],
                check=True,
                capture_output=True,
            )
            setup = Sandbox.start(
                project_info.image,
                run_id=run_id,
                deadline=deadline,
                egress=proxy,
                role="setup",
            )
            checkout.copy_into(setup, CHECKOUT)
            result = build_environment(
                setup,
                Budget(cap_usd=Decimal(1)),
                base_time=base_time,
                project=project_info,
            )
        elapsed = round(time.monotonic() - started, 1)
        if not result.ok:
            print(
                json.dumps(
                    {
                        "error": result.error,
                        "notes": result.notes,
                        "steps": [
                            step.log()
                            for step in result.steps
                            if step.kind
                            in {
                                "tooling",
                                "venv",
                                "install",
                                "build-tools",
                                "matplotlib-build-tool",
                                "matplotlib-build-config",
                            }
                            or step.kind.startswith("install-extra")
                        ],
                        "newer": [
                            item.log() for item in result.installed if item.newer
                        ],
                    },
                    indent=2,
                )
            )
        assert result.outcome == OUTCOME_SUCCEEDED, result.log()
        assert result.collect_count == expected_count
        print(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "python": result.python_version,
                    "collect_count": result.collect_count,
                    "duration_s": elapsed,
                }
            )
        )
    finally:
        if setup is not None:
            setup.kill()
        if proxy is not None:
            proxy.kill()
        cleanup(run_id)
