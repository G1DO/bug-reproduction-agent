"""Fail unless a pull request description fills in Problem and Acceptance check.

Reads the description from the PR_BODY environment variable. Guidance text in
the template is written as HTML comments, and lines that are only a label
(such as "Proven by:") don't count, so an untouched template fails.
"""

import os
import re
import sys

REQUIRED = ("Problem", "Acceptance check")


def sections(body: str) -> dict[str, str]:
    """Map each '## Heading' to the text under it."""
    found: dict[str, str] = {}
    current: str | None = None
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return found


def is_filled(text: str) -> bool:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines = [line.strip() for line in text.splitlines()]
    return any(line and not re.fullmatch(r"[^:]+:", line) for line in lines)


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    found = sections(body)
    missing = [name for name in REQUIRED if not is_filled(found.get(name, ""))]
    if missing:
        print("PR description must fill in: " + ", ".join(missing))
        return 1
    print("PR description has Problem and Acceptance check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
