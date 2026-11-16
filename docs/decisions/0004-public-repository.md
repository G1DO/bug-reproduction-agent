# 0004: Develop in a public repository

**Status:** Accepted (2026-11-16)

## Context

One of the project's purposes is public evidence of engineering skill. Readers must be able to recompute the published numbers from the published logs. Once a repository has been public, anything in it can't be taken back, because forks, clones and caches keep it.

Alternative considered: keep the repository private until the first backtest results exist, then make it public. Unfinished work stays hidden and mistakes can be cleaned up first. The trade-off: there's no public history of how the work was done, and making it public later still exposes the full history.

## Decision

The repository is public from the first commit.

## Consequences

- Never commit secrets. The gate scans the whole history for them. Turn on secret-scanning push protection.
- Vulnerabilities are reported privately: see `SECURITY.md`. Turn on private vulnerability reporting. A weakness in the sandbox stays private until it's fixed.
- Logs and results that get published must not contain credentials or anything that came from the person running the tool.
