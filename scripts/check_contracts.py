"""Breaking-change check for contract and schema paths.

Usage: check_contracts.py BASE_REF

Reads glob patterns from .github/contract-paths. Passes when no file matching
them changed since BASE_REF. When one did, it fails until a real
breaking-change check for that kind of contract is added here: no contract
may change unchecked.
"""

import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

PATTERNS_FILE = Path(".github/contract-paths")


def patterns() -> list[str]:
    lines = PATTERNS_FILE.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def changed_files(base: str) -> list[str]:
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    command = (
        ["git", "diff", "--name-only", f"{base}...HEAD"]
        if exists.returncode == 0
        else ["git", "ls-files"]
    )
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return output.split()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    globs = patterns()
    if not globs:
        print("No contract paths listed yet; nothing to check.")
        return 0
    hits = [f for f in changed_files(sys.argv[1]) if any(fnmatch(f, g) for g in globs)]
    for path in hits:
        print(f"contract changed with no breaking-change check for it yet: {path}")
    if hits:
        return 1
    print("No contract paths changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
