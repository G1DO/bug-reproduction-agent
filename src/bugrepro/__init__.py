"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from types import FrameType
from typing import Any

from bugrepro.model import (
    DEFAULT_COST_CAP_USD,
    DEFAULT_MAX_TOKENS,
    MIN_USEFUL_MAX_TOKENS,
    Budget,
    ModelClient,
    ModelError,
    ModelResult,
    Outcome,
)
from bugrepro.sandbox import (
    DEFAULT_TIME_LIMIT_S,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_STOPPED_AT_TIME_LIMIT,
    CleanupFailure,
    CommandResult,
    Deadline,
    DockerTooOld,
    DockerUnavailable,
    SandboxError,
    TimeLimitExceeded,
    cleanup,
    ensure_supported_docker,
    new_run_id,
    run_isolated,
)


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number of seconds") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def _max_tokens(value: str) -> int:
    try:
        tokens = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number of tokens") from exc
    if tokens < MIN_USEFUL_MAX_TOKENS:
        raise argparse.ArgumentTypeError(f"must be at least {MIN_USEFUL_MAX_TOKENS}")
    return tokens


def _usd_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a dollar amount") from exc
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return amount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugrepro",
        description="Turn bug reports into verified failing tests.",
    )
    commands = parser.add_subparsers(dest="command")

    sandbox = commands.add_parser(
        "sandbox",
        help="Developer tools for the Docker sandbox.",
    )
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command")
    run = sandbox_commands.add_parser(
        "run",
        help="Run one command in a fresh container with no network.",
        description=(
            "Developer tool, not the reproduce workflow. Runs one command in a "
            "fresh container with no network under the run's time limit."
        ),
    )
    run.add_argument("--image", required=True, help="Container image to run.")
    run.add_argument(
        "--time-limit",
        type=_positive_seconds,
        default=DEFAULT_TIME_LIMIT_S,
        metavar="SECONDS",
        help=f"Time limit for the whole run (default: {DEFAULT_TIME_LIMIT_S}).",
    )
    run.add_argument(
        "run_command",
        nargs=argparse.REMAINDER,
        help="Command to run, after --.",
    )

    model = commands.add_parser(
        "model",
        help="Call the model once under a cost cap.",
        description=(
            "Developer tool, not the reproduce workflow. Calls the model once "
            "under the run's cost cap and prints the run log."
        ),
    )
    model.add_argument("--prompt", required=True, help="Text to send to the model.")
    model.add_argument(
        "--max-tokens",
        type=_max_tokens,
        default=DEFAULT_MAX_TOKENS,
        metavar="N",
        help=f"Maximum output tokens (default: {DEFAULT_MAX_TOKENS}).",
    )
    model.add_argument(
        "--cost-cap",
        type=_usd_amount,
        default=DEFAULT_COST_CAP_USD,
        metavar="USD",
        help=(
            f"Most this run may spend, in US dollars (default: {DEFAULT_COST_CAP_USD})."
        ),
    )
    return parser


def _stop_on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _run_sandbox(args: argparse.Namespace) -> int:
    command = list(args.run_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("bugrepro: a command is required after --", file=sys.stderr)
        return 2

    run_id = new_run_id()
    deadline = Deadline(limit_s=args.time_limit)
    result: CommandResult | None = None
    failures: tuple[CleanupFailure, ...] = ()
    error: str | None = None
    interrupted = False

    previous_handler: Callable[[int, FrameType | None], Any] | int | None = None
    can_restore = False
    try:
        previous_handler = signal.signal(signal.SIGTERM, _stop_on_sigterm)
        can_restore = True
    except ValueError:
        pass

    try:
        ensure_supported_docker()
        result = run_isolated(
            args.image,
            command,
            timeout=deadline.remaining(),
            run_id=run_id,
            deadline=deadline,
        )
    except (DockerUnavailable, DockerTooOld, SandboxError) as exc:
        error = str(exc)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if can_restore:
            signal.signal(
                signal.SIGTERM,
                previous_handler if previous_handler is not None else signal.SIG_DFL,
            )
        failures = cleanup(run_id)

    for failure in failures:
        print(
            f"bugrepro: could not remove {failure.kind} {failure.object_id}: "
            f"{failure.error}",
            file=sys.stderr,
        )
    if error is not None:
        print(f"bugrepro: {error}", file=sys.stderr)
        return 2
    if interrupted:
        print("bugrepro: stopped", file=sys.stderr)
        return 130
    assert result is not None

    if result.timed_out:
        status = STATUS_STOPPED_AT_TIME_LIMIT
    elif result.exit_code == 0:
        status = STATUS_COMPLETED
    else:
        status = STATUS_FAILED

    print(
        json.dumps(
            {
                "status": status,
                "image": args.image,
                "time_limit_s": args.time_limit,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_s": result.duration_s,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
            },
            indent=2,
        )
    )
    if failures:
        return 1
    if result.timed_out:
        return 124
    return 0 if result.exit_code == 0 else 1


def _model_log(
    outcome: str,
    client: ModelClient,
    budget: Budget,
    *,
    result: ModelResult | None = None,
    error: str | None = None,
) -> dict[str, object]:
    usage = result.usage if result is not None else None
    return {
        "outcome": outcome,
        "model": (
            result.model
            if result is not None and result.model is not None
            else client.log_model
        ),
        "cost_cap_usd": float(budget.cap_usd),
        "cost_usd": sum(float(call.cost_usd) for call in budget.calls),
        "calls": budget.log(),
        "text": result.text if result is not None else None,
        "usage": (
            {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cache_hit_tokens": usage.cache_hit_tokens,
            }
            if usage is not None
            else None
        ),
        "finish_reason": result.finish_reason if result is not None else None,
        "error": (
            error
            if error is not None
            else (result.error if result is not None else None)
        ),
    }


def _run_model(args: argparse.Namespace) -> int:
    budget = Budget(cap_usd=args.cost_cap)
    try:
        client = ModelClient.from_environment(budget=budget)
    except ModelError as exc:
        print(f"bugrepro: {exc}", file=sys.stderr)
        return 2
    try:
        ensure_supported_docker()
        result = client.complete(
            [{"role": "user", "content": args.prompt}],
            max_tokens=args.max_tokens,
            deadline=Deadline(),
        )
    except TimeLimitExceeded as exc:
        print(
            json.dumps(
                _model_log("stopped-at-time-limit", client, budget, error=str(exc))
            )
        )
        return 1
    except (ModelError, SandboxError) as exc:
        print(f"bugrepro: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(_model_log(result.outcome.value, client, budget, result=result)))
    return 0 if result.outcome is Outcome.OK else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "model":
        return _run_model(args)
    if args.command == "sandbox" and args.sandbox_command == "run":
        return _run_sandbox(args)
    parser.print_help()
    return 0
