# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's private vulnerability reporting: go to this repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue, pull request or discussion about it.

The report stays private while it's fixed. A fix is published together with an advisory once a fixed version exists.

## Scope

The most important area is the sandbox. Code from bug reports runs in Docker containers, and it must not be able to:

- read credentials or files from the host,
- reach the network after its dependencies are installed,
- run past the per-issue time limit.

If you find a way to do any of these, please report it. Also report any way to make the tool write to GitHub, or to go over its per-issue spend cap.

## Supported versions

Nothing has been released yet. Fixes go to `main`.
