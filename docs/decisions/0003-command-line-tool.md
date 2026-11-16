# 0003: Ship as a command-line tool, not a service

**Status:** Accepted (2026-11-16)

## Context

The requirements say the tool is run by a developer, and its results go only to that person. It never writes to GitHub or contacts maintainers. Tying it to one project, maintainer opt-in, and shadow mode are all out of scope for now.

Alternative considered: a hosted service or GitHub App that watches repositories and responds to new issues. It would reproduce bugs without anyone running a command. The trade-off: it would need write access or a place to publish results, it would need hosting and operations, and it would run against the "results go only to the person running it" rule.

## Decision

The tool is a command-line program named `bugrepro`. It runs on the user's machine and uses the user's own Docker and model access.

## Consequences

- There's no deployment, no live service, and nothing to monitor while it runs. If that changes, it needs a new decision record.
- The command's flags and its output formats become a contract once anyone depends on them. Their paths go into `.github/contract-paths` when they're first defined.
- Costs are paid by whoever runs the tool, so the per-issue spend cap is enforced inside the tool.
