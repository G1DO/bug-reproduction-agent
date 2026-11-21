"""Verify a model-written test by collecting it once and running it three times.

The file must collect exactly one test item before the three runs. The test
must fail on all three runs with the test collected, and the failure must
contain the error type or message from the report. A test that raises the
report's message itself is not a reproduction. Every run starts a new container
from the setup image with no network.
"""

from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from bugrepro.languages.python import (
    CHECKOUT,
    collect_count,
    error_patterns,
    failure_match,
    is_error_message,
    is_exception_pattern,
    pytest_failure_counts,
    pytest_progress_counts,
)
from bugrepro.reproduce import ReproductionTest
from bugrepro.sandbox import (
    CommandResult,
    Deadline,
    SandboxError,
    SetupImage,
    TimeLimitExceeded,
    run_isolated,
)

VERIFY_RUNS: Final = 3
COLLECTION_ARGS: Final = ("--collect-only", "-q", "-p", "no:cacheprovider")
OUTCOME_REPRODUCED: Final = "reproduced"
OUTCOME_COULD_NOT_REPRODUCE: Final = "could-not-reproduce"
OUTCOME_STOPPED_AT_TIME_LIMIT: Final = "stopped-at-time-limit"
OUTCOME_ERROR: Final = "error"

ERROR_MATCH_MATCHED: Final = "matched"
ERROR_MATCH_SKIPPED: Final = "skipped"
ERROR_MATCH_NOT_MATCHED: Final = "not-matched"
ERROR_MATCH_NOT_CHECKED: Final = "not-checked"

NO_ERROR_IN_REPORT: Final = "no error message in report"


@dataclass(frozen=True)
class VerifyStep:
    """One run of the test in a fresh no-network container."""

    command: tuple[str, ...]
    output: str
    exit_code: int
    start: str
    duration_s: float
    timed_out: bool = False
    output_truncated: bool = False
    kind: str = "verify"

    def log(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "command": list(self.command),
            "output": self.output,
            "exit_code": self.exit_code,
            "start": self.start,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "output_truncated": self.output_truncated,
        }


@dataclass(frozen=True)
class ErrorMatch:
    """What the verification learned about the report's error."""

    status: str
    reason: str | None = None
    pattern: str | None = None

    def log(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "pattern": self.pattern,
        }


@dataclass(frozen=True)
class VerifyResult:
    """The verification verdict for one test."""

    outcome: str
    reason: str | None
    steps: tuple[VerifyStep, ...]
    test: ReproductionTest | None
    error_match: ErrorMatch
    image: SetupImage
    duration_s: float
    label: str | None = None
    error: str | None = None

    @property
    def reproduced(self) -> bool:
        return self.outcome == OUTCOME_REPRODUCED

    def log(self) -> dict[str, object]:
        result: dict[str, object] = {
            "outcome": self.outcome,
            "reason": self.reason,
            "image": self.image.ref,
            "test": self.test.log() if self.test is not None else None,
            "error_match": self.error_match.log(),
            "steps": [step.log() for step in self.steps],
            "duration_s": self.duration_s,
            "error": self.error,
        }
        if self.label is not None:
            result["label"] = self.label
        return result


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _output(result: CommandResult) -> str:
    parts = [result.stdout.rstrip("\n")]
    if result.stdout_truncated:
        parts.append("[stdout truncated]")
    if result.stderr.strip():
        parts.append(result.stderr.rstrip("\n"))
    if result.stderr_truncated:
        parts.append("[stderr truncated]")
    text = "\n".join(part for part in parts if part)
    if not text:
        return f"exit code {result.exit_code}"
    return text


def _step(
    command: tuple[str, ...], result: CommandResult, start: str, *, kind: str = "verify"
) -> VerifyStep:
    return VerifyStep(
        command=command,
        output=_output(result),
        exit_code=result.exit_code,
        start=start,
        duration_s=result.duration_s,
        timed_out=result.timed_out,
        output_truncated=result.stdout_truncated or result.stderr_truncated,
        kind=kind,
    )


def _frame_is_file(frame: str) -> bool:
    return not frame.startswith("<") and frame.endswith(".py")


def _same_file(frame: str, test_path: str) -> bool:
    frame_path = posixpath.normpath(frame.replace("\\", "/"))
    path = posixpath.normpath(test_path.replace("\\", "/"))
    return frame_path == path or frame_path.endswith("/" + path)


def _did_fail_as_a_test(result: CommandResult) -> bool:
    if result.exit_code != 1 or result.timed_out:
        return False
    output = _output(result)
    counts = pytest_failure_counts(output)
    progress = pytest_progress_counts(output)
    if counts is None or progress is None or counts != progress:
        return False
    failed, errors, passed, skipped = counts
    return failed == 1 and errors == 0 and failed + errors + passed + skipped == 1


def verify_test(
    image: SetupImage,
    test: ReproductionTest,
    *,
    report: str,
    run_id: str,
    deadline: Deadline,
    label: str | None = None,
) -> VerifyResult:
    """Collect the test once, then run it three times if it is one test."""
    started = time.monotonic()
    steps: list[VerifyStep] = []
    patterns = error_patterns(report)
    matched_pattern: str | None = None
    rejection_reason: str | None = None
    rejection_match: ErrorMatch | None = None

    def reject(reason: str, message: str) -> None:
        nonlocal rejection_reason, rejection_match
        if rejection_reason is None:
            rejection_reason = reason
            rejection_match = ErrorMatch(ERROR_MATCH_NOT_MATCHED, message)

    def finish(
        outcome: str,
        reason: str | None,
        *,
        error_match: ErrorMatch,
        test_returned: ReproductionTest | None,
        error: str | None = None,
    ) -> VerifyResult:
        return VerifyResult(
            outcome=outcome,
            reason=reason,
            steps=tuple(steps),
            test=test_returned,
            error_match=error_match,
            image=image,
            duration_s=round(time.monotonic() - started, 3),
            label=label,
            error=error,
        )

    try:
        deadline.check()
        start = _utc_now()
        collection_command = (*test.command, *COLLECTION_ARGS)
        result = run_isolated(
            image,
            collection_command,
            timeout=deadline.remaining(),
            run_id=run_id,
            deadline=deadline,
            workdir=CHECKOUT,
        )
        step = _step(collection_command, result, start, kind="collect")
        steps.append(step)

        if result.timed_out:
            return finish(
                OUTCOME_STOPPED_AT_TIME_LIMIT,
                "stopped-at-time-limit",
                error_match=ErrorMatch(
                    ERROR_MATCH_NOT_CHECKED,
                    "the collection run hit the time limit",
                ),
                test_returned=None,
                error=step.output,
            )
        if result.exit_code not in (0, 1, 2, 3, 4, 5):
            return finish(
                OUTCOME_ERROR,
                "sandbox-error",
                error_match=ErrorMatch(
                    ERROR_MATCH_NOT_CHECKED,
                    "the collection run did not finish",
                ),
                test_returned=None,
                error=step.output,
            )
        count: int | None = None
        if result.exit_code == 0:
            try:
                count = collect_count(step.output)
            except ValueError:
                count = None
        if count != 1:
            reject(
                "test-not-collected",
                "the framework did not collect exactly one test item",
            )
        else:
            for _ in range(VERIFY_RUNS):
                deadline.check()
                start = _utc_now()
                result = run_isolated(
                    image,
                    test.command,
                    timeout=deadline.remaining(),
                    run_id=run_id,
                    deadline=deadline,
                    workdir=CHECKOUT,
                )
                step = _step(test.command, result, start)
                steps.append(step)

                if result.timed_out:
                    return finish(
                        OUTCOME_STOPPED_AT_TIME_LIMIT,
                        "stopped-at-time-limit",
                        error_match=ErrorMatch(
                            ERROR_MATCH_NOT_CHECKED,
                            "the verification run hit the time limit",
                        ),
                        test_returned=None,
                        error=step.output,
                    )
                if result.exit_code not in (0, 1, 2, 3, 4, 5):
                    return finish(
                        OUTCOME_ERROR,
                        "sandbox-error",
                        error_match=ErrorMatch(
                            ERROR_MATCH_NOT_CHECKED,
                            "the verification run did not finish",
                        ),
                        test_returned=None,
                        error=step.output,
                    )
                if result.exit_code == 0:
                    reject("test-passed", "the test passed at least once")
                    continue
                if not _did_fail_as_a_test(result):
                    reject(
                        "test-not-collected",
                        "the test did not fail as a collected test",
                    )
                    continue
                if not patterns:
                    continue

                match = failure_match(step.output, patterns)
                if match is None:
                    reject(
                        "error-not-matched",
                        "no report error appears in the failure output",
                    )
                    continue
                pattern, frame = match
                if is_error_message(pattern) and pattern in test.content:
                    reject(
                        "error-message-in-test",
                        "the test contains the report's error message",
                    )
                if is_exception_pattern(pattern) and frame is not None:
                    if not _frame_is_file(frame):
                        reject(
                            "error-raised-outside-file",
                            "the matched error is raised outside a project file",
                        )
                    elif _same_file(frame, test.path):
                        reject(
                            "error-raised-in-test",
                            "the matched error is raised inside the test file",
                        )
                if rejection_reason is None and matched_pattern is None:
                    matched_pattern = pattern
    except TimeLimitExceeded as exc:
        return finish(
            OUTCOME_STOPPED_AT_TIME_LIMIT,
            "stopped-at-time-limit",
            error_match=ErrorMatch(
                ERROR_MATCH_NOT_CHECKED,
                "the run's time limit was reached",
            ),
            test_returned=None,
            error=str(exc),
        )
    except SandboxError as exc:
        return finish(
            OUTCOME_ERROR,
            "sandbox-error",
            error_match=ErrorMatch(
                ERROR_MATCH_NOT_CHECKED,
                "the sandbox failed during verification",
            ),
            test_returned=None,
            error=str(exc),
        )

    if rejection_reason is not None:
        assert rejection_match is not None
        return finish(
            OUTCOME_COULD_NOT_REPRODUCE,
            rejection_reason,
            error_match=rejection_match,
            test_returned=None,
        )

    if patterns:
        error_match = ErrorMatch(ERROR_MATCH_MATCHED, pattern=matched_pattern)
    else:
        error_match = ErrorMatch(ERROR_MATCH_SKIPPED, NO_ERROR_IN_REPORT)
    return finish(
        OUTCOME_REPRODUCED,
        None,
        error_match=error_match,
        test_returned=test,
    )
