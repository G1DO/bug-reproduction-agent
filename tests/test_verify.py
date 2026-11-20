from __future__ import annotations

import json
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import bugrepro.verify as verify_module
from bugrepro.languages.python import (
    CHECKOUT,
    VENV,
)
from bugrepro.languages.python import (
    test_command as make_test_command,
)
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
BARE_REPORT = "It raises ValueError."
IMAGE = SetupImage("fake-setup", "run-1")


def _test(path: str, content: str) -> ReproductionTest:
    return ReproductionTest(
        path=path,
        content=content,
        command=tuple(make_test_command(path)),
    )


def _pytest_output(
    tmp_path: Path,
    name: str,
    content: str,
    *,
    extra: Mapping[str, str] | None = None,
    output_args: Sequence[str] = ("-v", "--tb=long", "--color=no"),
    config: str | None = None,
) -> tuple[str, str]:
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    if config is not None:
        (tmp_path / "pytest.ini").write_text(config)
    if extra is not None:
        for filename, text in extra.items():
            (tests / filename).write_text(text)
    (tests / name).write_text(content)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *output_args, f"tests/{name}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return f"tests/{name}", completed.stdout + completed.stderr


def _library_raise_output(tmp_path: Path) -> tuple[str, str]:
    return _pytest_output(
        tmp_path,
        "test_repro.py",
        "import parser\n\ndef test_repro():\n    parser.parse('x')\n",
        extra={
            "parser.py": "def parse(text):\n    raise ValueError('bad value')\n",
        },
    )


def _assertion_output(tmp_path: Path) -> tuple[str, str]:
    return _pytest_output(
        tmp_path,
        "test_repro.py",
        "def test_repro():\n    assert False\n",
    )


def _failed(output: str, *, exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code, output, "", 0.01)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    results: Sequence[CommandResult],
    *,
    report: str = REPORT,
    test: ReproductionTest | None = None,
    label: str | None = None,
) -> tuple[
    VerifyResult,
    list[tuple[SetupImage, tuple[str, ...], float, str, Deadline, str]],
]:
    active_test = test or _test(
        "tests/test_repro.py", "def test_repro():\n    parser.parse('x')\n"
    )
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
        active_test,
        report=report,
        run_id="run-1",
        deadline=deadline,
        label=label,
    )
    return result, calls


def test_three_failing_runs_with_the_report_error_are_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _library_raise_output(tmp_path)
    test = _test(path, "import parser\n\ndef test_repro():\n    parser.parse('x')\n")
    result, calls = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=test,
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.reproduced
    assert result.test is test
    assert result.error_match.status == ERROR_MATCH_MATCHED
    assert result.error_match.pattern == "ValueError: bad value"
    assert len(result.steps) == 3
    assert len(calls) == 3
    assert all(call[0] == IMAGE for call in calls)
    assert all(call[1] == test.command for call in calls)
    assert all(call[2] > 0 for call in calls)
    assert all(call[3] == "run-1" for call in calls)
    assert all(call[5] == CHECKOUT for call in calls)


def test_error_type_alone_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _library_raise_output(tmp_path)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, "import parser\n\ndef test_repro():\n    parser.parse('x')\n"),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.status == ERROR_MATCH_MATCHED
    assert result.error_match.pattern == "ValueError"


def test_two_of_three_failures_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _library_raise_output(tmp_path)
    result, calls = _run(
        monkeypatch,
        [
            _failed(output),
            _failed(output),
            CommandResult(0, "1 passed in 0.01s\n", "", 0.01),
        ],
        test=_test(path, "import parser\n\ndef test_repro():\n    parser.parse('x')\n"),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-passed"
    assert result.test is None
    assert len(result.steps) == 3
    assert len(calls) == 3


def test_failure_without_the_report_error_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _assertion_output(tmp_path)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, "def test_repro():\n    assert False\n"),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None
    assert result.error_match.status == ERROR_MATCH_NOT_MATCHED


def test_error_match_ignores_the_test_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_ValueError():\n    assert False\n"
    path, output = _pytest_output(tmp_path, "test_ValueError.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None


def test_error_match_ignores_printed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "def test_repro():\n    print('ValueError: bad value')\n    assert False\n"
    )
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None


def test_error_match_ignores_the_test_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_repro():\n    assert False  # ValueError: bad value\n"
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None


def test_error_match_ignores_a_final_line_in_an_assertion_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "def test_repro():\n"
        "    assert (\n"
        "        '''\n"
        "tests/other.py:2: ValueError\n"
        "'''\n"
        "        == ''\n"
        "    )\n"
    )
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_output_after_the_pytest_report_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import atexit\n"
        "import parser\n"
        "\n"
        "\n"
        "def _fake():\n"
        '    print("FAILED tests/test_fake.py::test_fake - ValueError: bad value")\n'
        "\n"
        "\n"
        "atexit.register(_fake)\n"
        "\n"
        "\n"
        "def test_repro():\n"
        "    parser.divide()\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def divide():\n    return 1 / 0\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None


def test_passing_run_with_a_forged_failure_section_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import atexit\n"
        "import os\n"
        "\n"
        "\n"
        "def _fake():\n"
        '    print("=================================== FAILURES ===================================", flush=True)\n'
        '    print("E   ValueError: bad value", flush=True)\n'
        '    print("tests/test_repro.py:2: in test_repro", flush=True)\n'
        '    print("tests/parser.py:2: ValueError", flush=True)\n'
        '    print("1 failed in 0.01s", flush=True)\n'
        "    os._exit(1)\n"
        "\n"
        "\n"
        "atexit.register(_fake)\n"
        "\n"
        "\n"
        "def test_pass():\n"
        "    assert True\n"
    )
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_error_match_ignores_the_parametrize_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import pytest\n"
        "import parser\n"
        "\n"
        "\n"
        "@pytest.mark.parametrize(\n"
        "    'value',\n"
        "    [pytest.param(0, id=' - Value' + 'Error: bad ' + 'value')],\n"
        ")\n"
        "def test_repro(value):\n"
        "    parser.divide()\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def divide():\n    return 1 / 0\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-not-matched"
    assert result.test is None


def test_multi_test_file_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_selfraise():\n"
        "    raise ValueError('boom')\n"
        "\n"
        "\n"
        "def test_library():\n"
        "    parser.divide()\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def divide():\n    return 1 / 0\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_setup_error_is_not_a_reproduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('fixture broke')\n"
        "\n"
        "\n"
        "def test_repro(broken):\n"
        "    pass\n"
    )
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report="The parser drops the last line.",
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_chained_traceback_uses_the_matched_exceptions_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_repro():\n"
        "    try:\n"
        "        raise ValueError('bad value')\n"
        "    except ValueError:\n"
        "        parser.divide()\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def divide():\n    return 1 / 0\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_mixed_pass_and_fail_file_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_pass():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_library():\n"
        "    parser.parse('x')\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_multi_test_file_with_a_captured_footer_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_selfraise():\n"
        "    raise ValueError('bad value')\n"
        "\n"
        "\n"
        "def test_library():\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        '        print("1 failed in 0.01s")\n'
        "    parser.divide()\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def divide():\n    return 1 / 0\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_multi_test_file_with_a_printed_summary_header_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_a():\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        '        print("=========================== short test summary info ============================")\n'
        '        print("1 failed in 0.01s")\n'
        '    parser.parse("x")\n'
        "\n"
        "\n"
        "def test_b():\n"
        "    assert False\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "test-not-collected"
    assert result.test is None


def test_teardown_error_is_not_a_reproduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    yield\n"
        "    raise RuntimeError('teardown broke')\n"
        "\n"
        "\n"
        "def test_repro(broken):\n"
        "    assert True\n"
    )
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report="The parser drops the last line.",
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.test is None


def test_exec_raise_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_repro():\n    exec(\"raise ValueError('bad value')\")\n"
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-outside-file"
    assert result.test is None


def test_short_form_frames_in_captured_output_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "def test_repro():\n"
        "    print('src/other.py:9: in parse')\n"
        "    raise ValueError('boom')\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        output_args=("-v", "--tb=short", "--color=no"),
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_test_that_raises_the_report_message_itself_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_repro():\n    raise ValueError('ValueError: bad value')\n"
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-message-in-test"
    assert result.test is None


def test_failure_raised_in_the_test_file_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_repro():\n    raise ValueError('boom')\n"
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report=BARE_REPORT,
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_relative_dot_path_is_not_a_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "def test_repro():\n    raise ValueError('bad ' + 'value')\n"
    path, output = _pytest_output(tmp_path, "./test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test("tests/./test_repro.py", content),
    )

    assert path == "tests/./test_repro.py"
    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.reason == "error-raised-in-test"
    assert result.test is None


def test_indented_header_shaped_source_line_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_repro():\n"
        '    """\n'
        "    _test_\n"
        "    ----------------------------- Captured stdout call -----------------------------\n"
        '    """\n'
        "    parser.parse('x')\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.pattern == "ValueError: bad value"


def test_indented_footer_shaped_source_line_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = (
        "import parser\n"
        "\n"
        "\n"
        "def test_repro():\n"
        '    """\n'
        "    1 failed in 0.01s\n"
        '    """\n'
        "    parser.parse('x')\n"
    )
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.pattern == "ValueError: bad value"


def test_quiet_project_addopts_is_still_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "import parser\n\ndef test_repro():\n    parser.parse('x')\n"
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
        config="[pytest]\naddopts = -q\n",
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.pattern == "ValueError: bad value"


def test_verbose_project_addopts_is_still_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "import parser\n\ndef test_repro():\n    parser.parse('x')\n"
    path, output = _pytest_output(
        tmp_path,
        "test_repro.py",
        content,
        extra={"parser.py": "def parse(text):\n    raise ValueError('bad value')\n"},
        config="[pytest]\naddopts = -v\n",
    )
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=_test(path, content),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.pattern == "ValueError: bad value"


def test_report_without_an_error_message_skips_the_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _assertion_output(tmp_path)
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        report="The parser drops the last line.",
        test=_test(path, "def test_repro():\n    assert False\n"),
    )

    assert result.outcome == OUTCOME_REPRODUCED
    assert result.test is not None
    assert result.error_match.status == ERROR_MATCH_SKIPPED
    assert result.error_match.reason == NO_ERROR_IN_REPORT
    assert result.log()["error_match"] == {
        "status": ERROR_MATCH_SKIPPED,
        "reason": NO_ERROR_IN_REPORT,
        "pattern": None,
    }


def test_collection_error_is_not_a_reproduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "import missing_module\n\ndef test_repro():\n    pass\n"
    path, output = _pytest_output(tmp_path, "test_repro.py", content)
    result, _ = _run(
        monkeypatch,
        [_failed(output, exit_code=2) for _ in range(3)],
        test=_test(path, content),
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
        _test("tests/test_repro.py", "def test_repro():\n    assert False\n"),
        report=REPORT,
        run_id="run-1",
        deadline=Deadline(60),
    )

    assert result.outcome == OUTCOME_ERROR
    assert result.reason == "sandbox-error"
    assert result.test is None
    assert result.error == "docker was not found on PATH"


def test_could_not_reproduce_report_lists_every_step_with_its_command_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _assertion_output(tmp_path)
    test = _test(path, "def test_repro():\n    assert False\n")
    result, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=test,
    )

    assert result.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert result.test is None
    log = result.log()
    steps = cast(list[dict[str, object]], log["steps"])
    assert len(steps) == 3
    for step, expected in zip(steps, [output, output, output], strict=True):
        assert step["kind"] == "verify"
        assert step["command"] == list(test.command)
        assert step["output"] == expected.rstrip("\n")
        assert step["exit_code"] == 1
        assert isinstance(step["start"], str)
        assert step["start"]
        assert step["duration_s"] == 0.01


def test_result_line_carries_a_label_only_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, output = _library_raise_output(tmp_path)
    test = _test(path, "import parser\n\ndef test_repro():\n    parser.parse('x')\n")
    plain, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=test,
    )
    labelled, _ = _run(
        monkeypatch,
        [_failed(output) for _ in range(3)],
        test=test,
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


_POSITIVE_TEST = (
    "import socket\n"
    "import parser\n"
    "\n"
    "\n"
    "def test_repro():\n"
    '    print("HOSTNAME:" + socket.gethostname())\n'
    "    try:\n"
    '        socket.create_connection(("example.com", 443), 2)\n'
    "    except OSError:\n"
    '        print("NETWORK:blocked")\n'
    "    else:\n"
    '        print("NETWORK:connected")\n'
    '    parser.parse("x")\n'
)

_SELF_RAISE_TEST = (
    "import parser\n"
    "\n"
    "\n"
    "def test_selfraise():\n"
    "    try:\n"
    "        raise ValueError('bad value')\n"
    "    except ValueError:\n"
    "        parser.divide()\n"
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_existing.py").write_text("def test_existing():\n    assert True\n")
    (tests / "parser.py").write_text(
        "def parse(text):\n"
        "    raise ValueError('bad value')\n"
        "\n"
        "\n"
        "def divide():\n"
        "    return 1 / 0\n"
    )
    (tests / "test_selfraise.py").write_text(_SELF_RAISE_TEST)
    return project


def _copy_pytest(sandbox: Sandbox, staging: Path) -> None:
    purelib = Path(sysconfig.get_path("purelib"))
    for name in ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        source = purelib / name
        assert source.is_dir(), source
        shutil.copytree(source, staging / name)
    for name in ("pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        dist_info = list(purelib.glob(f"{name}-*.dist-info"))
        assert dist_info, f"{name} dist-info is missing"
        for entry in dist_info:
            shutil.copytree(entry, staging / entry.name)
    py = purelib / "py.py"
    assert py.is_file(), py
    shutil.copy2(py, staging / "py.py")
    created = sandbox.run(["python", "-m", "venv", "--without-pip", VENV])
    assert created.ok, created.stderr
    site = sandbox.run(
        [
            f"{VENV}/bin/python",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ]
    )
    assert site.ok, site.stderr
    destination = site.stdout.strip()
    assert destination
    sandbox.copy_in(staging, destination)


@pytest.mark.docker
def test_scripted_model_test_is_verified_in_three_new_no_network_containers(
    tmp_path: Path,
) -> None:
    run_id = new_run_id()
    deadline = Deadline(600)
    sandbox = Sandbox.start(PROXY_IMAGE, run_id=run_id, deadline=deadline, role="setup")
    result: VerifyResult | None = None
    negative: VerifyResult | None = None
    try:
        sandbox.copy_in(_project(tmp_path), CHECKOUT)
        staging = tmp_path / "packages"
        staging.mkdir()
        _copy_pytest(sandbox, staging)
        budget = Budget(cap_usd=Decimal(1))
        model = _ScriptedModel(
            [
                _action(
                    "write_test",
                    path="tests/test_repro.py",
                    content=_POSITIVE_TEST,
                ),
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
        assert reproduced.test.command == tuple(
            make_test_command("tests/test_repro.py")
        )
        result = verify_test(
            reproduced.image,
            reproduced.test,
            report=REPORT,
            run_id=run_id,
            deadline=deadline,
        )
        negative = verify_test(
            reproduced.image,
            _test("tests/test_selfraise.py", _SELF_RAISE_TEST),
            report=BARE_REPORT,
            run_id=run_id,
            deadline=deadline,
        )
    finally:
        cleanup(run_id)

    assert result is not None
    assert result.outcome == OUTCOME_REPRODUCED, result.log()
    assert result.test is not None
    assert len(result.steps) == 3
    assert all(step.command == result.test.command for step in result.steps)
    hosts: list[str] = []
    for step in result.steps:
        assert "NETWORK:blocked" in step.output
        assert "parser.parse" in step.output
        host = next(
            line.split(":", 1)[1]
            for line in step.output.splitlines()
            if line.startswith("HOSTNAME:")
        )
        hosts.append(host)
    assert len(set(hosts)) == 3
    assert negative is not None
    assert negative.outcome == OUTCOME_COULD_NOT_REPRODUCE
    assert negative.reason == "error-raised-in-test"
    assert negative.test is None
