# 0001: Write the tool in Python

**Status:** Accepted (2026-11-16)

## Context

The tool is a command-line program. It reads bug reports, drives a language model, builds project environments, and runs tests in sandboxes. The projects it works on can be in any of eight languages: Python, Java, TypeScript, JavaScript, Go, Rust, C and C++. The tool's own language doesn't have to match any of them, because target code only ever runs inside a sandbox.

The harnesses for two of the answer keys, SWT-bench and Multi-SWE-bench, are written in Python. Results have to be scored with their criteria.

Alternative considered: Go. It ships as a single static binary with no runtime to install, and it's strong at process and container control. The trade-off: Go would be further from the Python benchmark harnesses and from most language-model tooling, so scoring and model integration would need more glue code.

## Decision

The tool is written in Python. It's packaged with uv's packaged layout: code in `src/bugrepro/`, exact versions in `uv.lock`.

## Consequences

- Users need a Python runtime, or a tool such as `uvx` that supplies one.
- The benchmark harnesses can be called directly or followed closely, which keeps scoring comparable with published numbers.
- Supported Python versions are set in `pyproject.toml` (`requires-python`).
