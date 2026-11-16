"""Fail on known vulnerabilities in uv.lock that the base revision didn't have.

Usage: check_new_vulns.py BASE_REF

Audits the working tree's lockfile and BASE_REF's lockfile with `uv audit`
(OSV database) and fails only on (package, advisory) pairs that are new.
Vulnerabilities already on the base are left to the dependency bot. If
BASE_REF or its lockfile doesn't exist, every finding counts as new.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

AUDIT = [
    "uv",
    "audit",
    "--output-format",
    "json",
    "--preview-features",
    "audit-command,json-output",
]

Finding = tuple[str, str]


def audit(project: Path, *mode: str) -> set[Finding]:
    result = subprocess.run(
        [*AUDIT, *mode], cwd=project, capture_output=True, text=True, check=False
    )
    # uv audit exits 0 when clean and 1 when it found vulnerabilities.
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stderr)
        raise SystemExit(f"uv audit failed in {project} (exit {result.returncode})")
    report = json.loads(result.stdout)
    return {(v["dependency"]["name"], v["id"]) for v in report["vulnerabilities"]}


def git_show(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def base_findings(ref: str) -> set[Finding]:
    lock = git_show(ref, "uv.lock")
    project = git_show(ref, "pyproject.toml")
    if lock is None or project is None:
        print(f"No lockfile at {ref}; every finding counts as new.")
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "uv.lock").write_text(lock)
        Path(tmp, "pyproject.toml").write_text(project)
        return audit(Path(tmp), "--frozen")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    new = audit(Path.cwd(), "--locked") - base_findings(sys.argv[1])
    for name, advisory in sorted(new):
        print(f"new vulnerable dependency: {name} ({advisory})")
    if new:
        return 1
    print("No new vulnerable dependencies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
