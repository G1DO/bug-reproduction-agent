"""The gate's own checks, run the way CI runs them."""

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TEMPLATE = Path(__file__).resolve().parent.parent / ".github/pull_request_template.md"


def run(script: str, *args: str, env: dict[str, str] | None = None) -> int:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        check=False,
    ).returncode


def test_untouched_pr_template_fails() -> None:
    assert run("check_pr_body.py", env={"PR_BODY": TEMPLATE.read_text()}) == 1


def test_pr_body_missing_acceptance_check_fails() -> None:
    body = "## Problem\n\nUsers can't X.\n\n## Acceptance check\n\nProven by:\n"
    assert run("check_pr_body.py", env={"PR_BODY": body}) == 1


def test_filled_pr_body_passes() -> None:
    body = (
        TEMPLATE.read_text()
        .replace("<!-- The user problem this solves. -->", "Users can't X.")
        .replace("<!-- The observable result that shows it works. -->", "X works.")
    )
    assert run("check_pr_body.py", env={"PR_BODY": body}) == 0


def test_tag_pinned_action_fails(tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text("steps:\n  - uses: actions/checkout@v7\n")
    assert run("check_action_pins.py", str(tmp_path)) == 1


def test_sha_pinned_action_passes(tmp_path: Path) -> None:
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    (tmp_path / "ci.yml").write_text(f"steps:\n  - uses: actions/checkout@{sha}\n")
    assert run("check_action_pins.py", str(tmp_path)) == 0
