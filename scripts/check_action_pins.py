"""Fail unless every action and container a workflow uses is pinned immutably.

Actions must be pinned to a full 40-character commit SHA; docker:// images
must be pinned to a sha256 digest. Local actions (./path) are allowed.
"""

import re
import sys
from pathlib import Path

USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)")
ACTION_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
IMAGE_PIN = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")


def unpinned(workflow_dir: Path) -> list[str]:
    problems = []
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = USES.match(line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith("./"):
                continue
            pattern = IMAGE_PIN if ref.startswith("docker://") else ACTION_PIN
            if not pattern.match(ref):
                problems.append(f"{path}:{number}: {ref}")
    return problems


def main() -> int:
    workflow_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows")
    problems = unpinned(workflow_dir)
    for problem in problems:
        print(f"not pinned to a full commit SHA or digest: {problem}")
    if problems:
        return 1
    print("All workflow actions are pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
