"""Build a Python project's environment inside the sandbox.

The deterministic recipe comes from the one-day spike's write-up: a full
``python:<minor>`` image pinned by digest, ``uv`` with an exact
``--exclude-newer`` bound, non-editable first, and collection as the success
test.  The model is only called when collection still fails.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import Message
from typing import IO, Final, cast

from bugrepro.egress_proxy import Phase, normalize_host
from bugrepro.model import Budget, ModelClient, ModelError, Outcome
from bugrepro.sandbox import (
    Checkout,
    CommandResult,
    Deadline,
    Sandbox,
    SandboxError,
    SetupFailed,
    TimeLimitExceeded,
)

OUTCOME_SUCCEEDED: Final = "setup-succeeded"
OUTCOME_FAILED: Final = "setup-failed"
OUTCOME_STOPPED_AT_TIME_LIMIT: Final = "stopped-at-time-limit"
OUTCOME_STOPPED_AT_COST_CAP: Final = "stopped-at-cost-cap"

CHECKOUT: Final = "/tmp/checkout"
VENV: Final = "/tmp/venv"
UV_BIN: Final = "/tmp/.local/bin/uv"
BUILD_TOOLS: Final = "/tmp/bugrepro-build-tools"
UV_VERSION: Final = "0.13.7"
CMEEL_QHULL_VERSION: Final = "8.0.2.1"
MPL_BUILD_TOOLS: Final = "/tmp/mpl-build"
MPL_CONFIG: Final = "/tmp/mplsetup.cfg"
DEFAULT_PYTHON: Final = "3.10"
PYPI_JSON: Final = "https://pypi.org/pypi"
REGISTRY_ATTEMPTS: Final = 3
MAX_REGISTRY_BYTES: Final = 8 * 1024 * 1024
MODEL_TURNS: Final = 2
MODEL_COMMANDS_PER_TURN: Final = 5
MAX_MODEL_ARG: Final = 4096
MAX_STEP_OUTPUT_CHARS: Final = 4000

PYTHON_IMAGES: Final = {
    "3.8": (
        "python:3.8@sha256:"
        "d411270700143fa2683cc8264d9fa5d3279fd3b6afff62ae81ea2f9d070e390c"
    ),
    "3.9": (
        "python:3.9@sha256:"
        "da5aee29682d12a6649f51c8d6f15b87deb3e6c524b923c41d0cb3304d07c913"
    ),
    "3.10": (
        "python:3.10@sha256:"
        "c10791e6f69086d5216f7b5d3b839929c454e24305867ed9bc5152f0355b5423"
    ),
}

PYTHON_RELEASES: Final = {
    "3.8": date(2019, 10, 14),
    "3.9": date(2020, 10, 5),
    "3.10": date(2021, 10, 4),
}

REGISTRIES: Final = frozenset({"pypi.org", "files.pythonhosted.org"})

_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_VERSION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+!_-]{0,127}")
_EXCEPTION_NAME_PATTERN: Final = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning)\b"
)
_TRACEBACK_MARKER: Final = "Traceback (most recent call last):"
_PYTEST_FRAME_PATTERNS: Final = (re.compile(r"^(?P<path>.+?\.py):\d+: in "),)
_PYTEST_ERROR_LINE: Final = re.compile(r"^E\s+(?P<text>.+)$")
_PYTEST_FINAL_LINE: Final = re.compile(
    r"^(?P<path>.+?):\d+: (?P<type>[A-Za-z_][A-Za-z0-9_.]*)(?:: .*)?$"
)
_PYTEST_FOOTER: Final = re.compile(
    r"^\s*=*\s*\d+ (?:failed|passed|error|warning|skipped)"
    r"(?:, \d+ (?:failed|passed|error|warning|skipped))* in \S"
)
_PYTEST_COUNT: Final = re.compile(r"(\d+) (failed|passed|error|warning|skipped)")
_MODEL_ALLOWED: Final = frozenset({"uv", "python", "python3", "pytest"})
_MODEL_REJECTED_PARTS: Final = (
    "git+",
    "git://",
    "hg+",
    "svn+",
    "bzr+",
    "ssh://",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
)
_MODEL_REJECTED_CHARS: Final = frozenset(";|&><`$\n\r\x00")
_MODEL_ALLOWED_PREFIXES: Final = (
    "/tmp/checkout",
    "/tmp/venv",
    "/tmp/.local",
    "/tmp/uv-cache",
)


class AuditError(Exception):
    """The post-setup dependency audit could not finish."""

    def __init__(self, message: str, *, reason: str = "audit-failed") -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class SetupStep:
    """One command run during setup, or the metadata inspection."""

    kind: str
    command: tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def log(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "command": list(self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }


@dataclass(frozen=True)
class UrlDependency:
    """A direct URL dependency found in project metadata."""

    requirement: str
    url: str
    host: str


@dataclass(frozen=True)
class ProjectInfo:
    """What the trusted metadata pass learned about the project."""

    python_version: str
    image: str
    requires_python: str | None
    project_name: str | None
    project_version: str | None
    uses_scm: bool
    extras: tuple[str, ...]
    extra_dependencies: dict[str, tuple[str, ...]]
    django_settings: str | None
    django_suite: bool
    matplotlib_build: bool
    importlib_mode: bool
    git_dependencies: tuple[str, ...]
    url_dependencies: tuple[UrlDependency, ...]
    sources: tuple[str, ...]
    inspection: SetupStep | None = None


@dataclass(frozen=True)
class UnboundedDependency:
    """A dependency the date bound cannot cover."""

    kind: str
    requirement: str
    host: str | None = None

    def log(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "requirement": self.requirement,
            "host": self.host,
        }


@dataclass(frozen=True)
class InstalledPackage:
    """One package in the setup venv and what its registry says."""

    name: str
    version: str
    upload_time: str | None
    source: str
    host: str | None = None
    newer: bool = False
    unbounded: bool = False

    def log(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "upload_time": self.upload_time,
            "source": self.source,
            "host": self.host,
            "newer": self.newer,
            "unbounded": self.unbounded,
        }


@dataclass(frozen=True)
class Distribution:
    """One installed distribution, as reported by the venv."""

    name: str
    version: str
    direct_url: dict[str, object] | None = None


@dataclass(frozen=True)
class SetupResult:
    """The whole setup outcome for one issue run."""

    outcome: str
    reason: str | None
    image: str
    python_version: str
    base_time: str
    steps: tuple[SetupStep, ...]
    installed: tuple[InstalledPackage, ...] = ()
    unbounded: tuple[UnboundedDependency, ...] = ()
    collect_count: int | None = None
    notes: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_SUCCEEDED

    def log(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "image": self.image,
            "python_version": self.python_version,
            "base_time": self.base_time,
            "dependency_bound": {
                "ecosystem": "pypi",
                "kind": "exclude-newer",
                "time": self.base_time,
                "reinstall": True,
            },
            "unbounded": [item.log() for item in self.unbounded],
            "installed": [item.log() for item in self.installed],
            "newer": [item.log() for item in self.installed if item.newer],
            "collect_count": self.collect_count,
            "notes": list(self.notes),
            "steps": [step.log() for step in self.steps],
            "error": self.error,
        }


_INSPECT_SCRIPT: Final = r"""
import configparser
import json
import re
import sys
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(sys.argv[1])
RESOLVED_ROOT = ROOT.resolve()
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 200
read_count = 0
sources = []
versions = []
pinned = None
requires = None
project_name = None
project_version = None
uses_scm = False
extras = []
extra_deps = {}
deps = []
django_settings = None
django_suite = False
matplotlib_build = False
importlib_mode = False


def safe_text(relative):
    global read_count
    if read_count >= MAX_FILES:
        return None
    path = (ROOT / relative).resolve()
    try:
        path.relative_to(RESOLVED_ROOT)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        read_count += 1
        sources.append(str(path.relative_to(RESOLVED_ROOT)))
        return path.read_text("utf-8", "replace")
    except OSError:
        return None


def add_versions(text):
    if not text:
        return
    for match in re.finditer(r"(?<![\w.])(\d+\.\d+)(?![\w.])", text):
        value = match.group(1)
        if value not in versions:
            versions.append(value)
    for match in re.finditer(r"(?<![\w.])py(\d)(\d+)(?![\w.])", text):
        value = match.group(1) + "." + match.group(2)
        if value not in versions:
            versions.append(value)


def add_dep(value):
    if not value:
        return
    for line in re.split(r"[\n,]", value):
        line = line.strip()
        if line and not line.startswith("#"):
            deps.append(line)


def add_extra(name, values):
    key = str(name).strip()
    if not key:
        return
    if key not in extras:
        extras.append(key)
    bucket = extra_deps.setdefault(key, [])
    for value in values:
        text = str(value).strip()
        if text:
            add_dep(text)
            if text not in bucket:
                bucket.append(text)


pyproject = safe_text("pyproject.toml")
if pyproject:
    try:
        data = tomllib.loads(pyproject)
    except ValueError:
        data = {}
    project = data.get("project", {})
    if isinstance(project, dict):
        requires = project.get("requires-python") or requires
        project_name = project.get("name") or project_name
        project_version = project.get("version") or project_version
        classifiers = project.get("classifiers", [])
        if isinstance(classifiers, list):
            add_versions(" ".join(str(item) for item in classifiers))
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            for item in dependencies:
                add_dep(str(item))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for key, values in optional.items():
                if isinstance(values, list):
                    add_extra(key, values)
        dynamic = project.get("dynamic", [])
        if isinstance(dynamic, list) and "version" in dynamic:
            tool = data.get("tool", {})
            if isinstance(tool, dict) and "setuptools_scm" in tool:
                uses_scm = True
    build = data.get("build-system", {})
    if isinstance(build, dict):
        build_requires = build.get("requires", [])
        if isinstance(build_requires, list):
            for item in build_requires:
                add_dep(str(item))
    tool = data.get("tool", {})
    if isinstance(tool, dict):
        if "setuptools_scm" in tool:
            uses_scm = True
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_deps = poetry.get("dependencies", {})
            if isinstance(poetry_deps, dict) and poetry_deps.get("python"):
                requires = str(poetry_deps["python"])
            poetry_extras = poetry.get("extras", {})
            if isinstance(poetry_extras, dict):
                for key, values in poetry_extras.items():
                    if isinstance(values, list):
                        add_extra(key, values)
        uv = tool.get("uv", {})
        if isinstance(uv, dict):
            sources_map = uv.get("sources", {})
            if isinstance(sources_map, dict):
                for source in sources_map.values():
                    if not isinstance(source, dict):
                        continue
                    if "git" in source:
                        deps.append("git+" + str(source["git"]))
                    if "url" in source:
                        deps.append(str(source["url"]))

setup_cfg = safe_text("setup.cfg")
if setup_cfg:
    config = configparser.ConfigParser()
    try:
        config.read_string(setup_cfg)
    except configparser.Error:
        config = configparser.ConfigParser()
    if config.has_section("options"):
        requires = config.get("options", "python_requires", fallback=requires)
        add_dep(config.get("options", "install_requires", fallback=""))
    if config.has_section("options.extras_require"):
        for key, value in config.items("options.extras_require"):
            add_extra(key, value.splitlines())
    if config.has_section("metadata"):
        project_name = config.get("metadata", "name", fallback=project_name)
        project_version = config.get("metadata", "version", fallback=project_version)
        add_versions(config.get("metadata", "classifiers", fallback=""))

setup_py = safe_text("setup.py")
if setup_py:
    match = re.search(r"python_requires\s*=\s*['\"]([^'\"]+)", setup_py)
    if match:
        requires = match.group(1)
    match = re.search(r"version\s*=\s*['\"]([^'\"]+)", setup_py)
    if match:
        project_version = match.group(1)
    match = re.search(r"name\s*=\s*['\"]([^'\"]+)", setup_py)
    if match:
        project_name = match.group(1)
    if "setuptools_scm" in setup_py or "use_scm_version" in setup_py:
        uses_scm = True
    match = re.search(r"install_requires\s*=\s*\[(.*?)\]", setup_py, re.S)
    if match:
        for item in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)):
            add_dep(item)
    match = re.search(r"extras_require\s*=\s*\{(.*?)\n\s*\}", setup_py, re.S)
    if match:
        for item in re.finditer(
            r"['\"]([^'\"]+)['\"]\s*:\s*\[(.*?)\]", match.group(1), re.S
        ):
            values = re.findall(r"['\"]([^'\"]+)['\"]", item.group(2))
            add_extra(item.group(1), values)

if project_name:
    package_dir = project_name.replace("-", "_").lower()
    if (ROOT / "lib" / package_dir).is_dir():
        importlib_mode = True

python_version = safe_text(".python-version")
if python_version:
    match = re.search(r"\b(\d+)\.(\d+)", python_version)
    if match:
        pinned = match.group(1) + "." + match.group(2)

tox = safe_text("tox.ini")
if tox:
    config = configparser.ConfigParser()
    try:
        config.read_string(tox)
    except configparser.Error:
        config = configparser.ConfigParser()
    if config.has_section("tox"):
        add_versions(config.get("tox", "envlist", fallback=""))
    for section in config.sections():
        if section == "testenv" or section.startswith("testenv:"):
            add_versions(config.get(section, "basepython", fallback=""))

ci_paths = []
for pattern in (
    ".travis.yml",
    "appveyor.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
):
    ci_paths.append(pattern)
workflows = ROOT / ".github" / "workflows"
if workflows.is_dir():
    for path in sorted(workflows.glob("*.y*ml")):
        ci_paths.append(str(path.relative_to(ROOT)))
for path in ci_paths:
    text = safe_text(path)
    if not text:
        continue
    for line in text.splitlines():
        if "dev" in line.lower():
            continue
        for regex in (
            r"(?:python-version|python_version|PYTHON_VERSION)\s*:\s*['\"]?(\d+\.\d+)",
            r"^\s*-\s*python\s*:\s*['\"]?(\d+\.\d+)",
        ):
            for match in re.finditer(regex, line, re.M):
                value = match.group(1)
                if value not in versions:
                    versions.append(value)

setupext = safe_text("setupext.py")
if setupext and "LOCAL_FREETYPE_VERSION" in setupext:
    matplotlib_build = True

if (ROOT / "tests" / "runtests.py").is_file():
    django_suite = True
if (ROOT / "tests" / "test_sqlite.py").is_file():
    django_settings = "tests.test_sqlite"
elif (ROOT / "manage.py").is_file():
    manage = safe_text("manage.py") or ""
    match = re.search(
        r"DJANGO_SETTINGS_MODULE['\"]?\s*[,=:]\s*['\"]([^'\"]+)", manage
    )
    if match:
        django_settings = match.group(1)

git_dependencies = []
url_dependencies = []
seen_urls = set()
for value in deps:
    lowered = value.lower()
    requirement, _, _ = value.partition("@")
    requirement = requirement.strip()[:500]
    if any(marker in lowered for marker in (
        "git+",
        "git://",
        "hg+",
        "svn+",
        "bzr+",
        "ssh://",
        "github.com",
        "gitlab.com",
        "bitbucket.org",
    )):
        if value[:1000] not in git_dependencies:
            git_dependencies.append(value[:1000])
    for match in re.finditer(r"https?://[^\s'\"\\,]+", value):
        url = match.group(0).rstrip(".)]")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        host = urllib.parse.urlsplit(url).hostname or ""
        if any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("github.com", "gitlab.com", "bitbucket.org")
        ):
            if value[:1000] not in git_dependencies:
                git_dependencies.append(value[:1000])
            continue
        url_dependencies.append(
            {"requirement": requirement or url, "url": url, "host": host}
        )

print(json.dumps({
    "python_versions": versions,
    "pinned_python": pinned,
    "requires_python": requires,
    "project_name": project_name,
    "project_version": project_version,
    "uses_scm": uses_scm,
    "extras": list(dict.fromkeys(extras)),
    "extra_dependencies": extra_deps,
    "django_settings": django_settings,
    "django_suite": django_suite,
    "matplotlib_build": matplotlib_build,
    "importlib_mode": importlib_mode,
    "git_dependencies": git_dependencies,
    "url_dependencies": url_dependencies,
    "sources": sources,
}))
"""

_DJANGO_COLLECT_SCRIPT: Final = r"""
import os
import sys

root = sys.argv[1]
tests_dir = os.path.join(root, "tests")
sys.path.insert(0, root)
sys.path.insert(0, tests_dir)
os.chdir(tests_dir)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.test_sqlite")

import tests.runtests as runtests
from django.conf import settings
from django.test.utils import get_runner

runtests.setup(0, [], 1, None, None)
settings.TEST_RUNNER = "django.test.runner.DiscoverRunner"
runner_class = get_runner(settings)
runner = runner_class(verbosity=0, interactive=False)
suite = runner.build_suite(runtests.get_installed())
print("COUNT=%d" % suite.countTestCases())
"""

_LIST_SCRIPT: Final = r"""
import importlib.metadata
import json

rows = []
for dist in importlib.metadata.distributions():
    try:
        name = dist.metadata["Name"]
        version = dist.version
    except Exception:
        continue
    direct = None
    try:
        direct = dist.read_text("direct_url.json")
    except Exception:
        pass
    rows.append({"name": name, "version": version, "direct_url": direct})
print(json.dumps({"rows": rows}))
"""


def registries() -> frozenset[str]:
    """Return the hosts the install phase may reach."""
    return REGISTRIES


def test_command(test_path: str) -> list[str]:
    """Return the one command that runs one test with the venv's pytest."""
    if not test_path:
        raise ValueError("a test path is required")
    return [
        f"{VENV}/bin/python",
        "-m",
        "pytest",
        "-q",
        "--tb=long",
        "--color=no",
        test_path,
    ]


def collect_count(output: str) -> int:
    """Parse pytest's collection output and return the item count."""
    patterns = (
        r"(\d+)\s+tests?\s+collected",
        r"collected\s+(\d+)\s+items?",
        r"COUNT=(\d+)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, output)
        if matches:
            return int(matches[-1])
    node_ids = {
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.strip().startswith(("=", "collected"))
    }
    if node_ids:
        return len(node_ids)
    raise ValueError("pytest did not report a collection count")


def error_patterns(report: str) -> tuple[str, ...]:
    """Return Python exception names and the last error line in a report.

    The last error line is read from a traceback or code block. A report of
    plain prose with no exception names yields no patterns, so an absent error
    message can be reported as skipped.
    """
    patterns: list[str] = []
    for match in _EXCEPTION_NAME_PATTERN.finditer(report):
        value = match.group(0)
        if value not in patterns:
            patterns.append(value)
    fenced = re.findall(r"```[^\n]*\n(.*?)```", report, re.DOTALL)
    if fenced:
        lines = fenced[-1].splitlines()
    elif _TRACEBACK_MARKER in report:
        lines = report.split(_TRACEBACK_MARKER, 1)[1].splitlines()
    else:
        lines = report.splitlines()
    for line in reversed(lines):
        candidate = line.strip()
        if not candidate or len(candidate) > 300:
            continue
        if any(ord(char) < 0x20 for char in candidate):
            continue
        error_match = _EXCEPTION_NAME_PATTERN.search(candidate)
        if error_match is None:
            continue
        after = candidate[error_match.end() : error_match.end() + 1]
        if after not in ("", ":", " ", "\t"):
            continue
        error_line = candidate[error_match.start() :]
        if error_line not in patterns:
            patterns.append(error_line)
        break
    return tuple(patterns[:20])


def is_exception_name(value: str) -> bool:
    """Return whether a pattern is a bare Python exception class name."""
    return _EXCEPTION_NAME_PATTERN.fullmatch(value) is not None


def is_error_message(value: str) -> bool:
    """Return whether a pattern is an exception message, not just a class name."""
    return _EXCEPTION_NAME_PATTERN.search(value) is not None and not is_exception_name(
        value
    )


def is_exception_pattern(value: str) -> bool:
    """Return whether a pattern names a Python exception."""
    return _EXCEPTION_NAME_PATTERN.search(value) is not None


def _failures_header(line: str) -> bool:
    return line.startswith("=") and "FAILURES" in line


def _errors_header(line: str) -> bool:
    return line.startswith("=") and "ERRORS" in line


def _is_captured_header(line: str) -> bool:
    return line.startswith("-") and "Captured" in line


def _is_test_header(line: str) -> bool:
    stripped = line.rstrip()
    return (
        stripped.startswith("_")
        and stripped.endswith("_")
        and any(char not in "_ " for char in stripped)
    )


def _is_summary_header(line: str) -> bool:
    return line.startswith("=") and "short test summary info" in line


def _real_footer_index(output: str) -> int | None:
    lines = output.splitlines()
    in_captured = False
    for index, line in enumerate(lines):
        if _is_captured_header(line):
            in_captured = True
            continue
        if in_captured:
            if _is_test_header(line) or _is_summary_header(line):
                in_captured = False
            else:
                continue
        if _PYTEST_FOOTER.match(line):
            return index
    return None


def _before_footer(output: str) -> str:
    index = _real_footer_index(output)
    if index is None:
        return output
    return "\n".join(output.splitlines()[:index])


def pytest_failure_counts(output: str) -> tuple[int, int, int, int] | None:
    """Return (failed, errors, passed, skipped) from pytest's real footer."""
    index = _real_footer_index(output)
    if index is None:
        return None
    line = output.splitlines()[index]
    counts = {name: int(count) for count, name in _PYTEST_COUNT.findall(line)}
    return (
        counts.get("failed", 0),
        counts.get("error", 0),
        counts.get("passed", 0),
        counts.get("skipped", 0),
    )


def pytest_progress_counts(output: str) -> tuple[int, int, int, int] | None:
    """Return (failed, errors, passed, skipped) from pytest's progress output."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _failures_header(line) or _errors_header(line):
            lines = lines[:index]
            break

    verbose: list[str] = []
    for line in lines:
        match = re.match(
            r"^.+::.+ (?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            line,
        )
        if match is not None:
            verbose.append(match.group("status"))
    if verbose:
        return (
            verbose.count("FAILED"),
            verbose.count("ERROR"),
            verbose.count("PASSED"),
            verbose.count("SKIPPED"),
        )

    for line in lines:
        match = re.match(r"^\S+\.py (?P<progress>[.FEsxX]+)(?:\s|\[|$)", line)
        if match is None:
            continue
        progress = match.group("progress")
        return (
            progress.count("F"),
            progress.count("E"),
            progress.count("."),
            progress.count("s"),
        )

    for line in lines:
        match = re.match(r"^([.FEsxX]+)(?:\s|\[|$)", line)
        if match is None:
            continue
        progress = match.group(1)
        return (
            progress.count("F"),
            progress.count("E"),
            progress.count("."),
            progress.count("s"),
        )
    return None


def _pytest_sections(output: str) -> list[str]:
    """Return the first real failure block, before any footer."""
    lines = _before_footer(output).splitlines()
    failure_start: int | None = None
    for index, line in enumerate(lines):
        if _failures_header(line):
            failure_start = index
            break
    if failure_start is None:
        return []

    block_start = failure_start + 1
    for index in range(failure_start + 1, len(lines)):
        if _is_test_header(lines[index]):
            block_start = index + 1
            break

    failure: list[str] = []
    for line in lines[block_start:]:
        if (
            _is_test_header(line)
            or _is_captured_header(line)
            or _is_summary_header(line)
        ):
            break
        failure.append(line)
    return failure


def failure_evidence(output: str) -> tuple[tuple[str, str | None], ...]:
    """Return each exception's text and the frame that raised it."""
    failure = _pytest_sections(output)
    evidence: list[tuple[str, str | None]] = []
    current: str | None = None
    for line in failure:
        match = _PYTEST_ERROR_LINE.match(line)
        if match is not None:
            if current is None:
                current = match.group("text").strip()
            continue
        final = _PYTEST_FINAL_LINE.match(line)
        if final is not None and current is not None:
            evidence.append(
                (
                    current + "\n" + final.group("type"),
                    final.group("path"),
                )
            )
            current = None
    if current is not None:
        evidence.append(
            (
                current,
                _frame_before(failure, current, _PYTEST_FRAME_PATTERNS),
            )
        )
    return tuple(evidence)


def failure_match(
    output: str, patterns: Sequence[str]
) -> tuple[str, str | None] | None:
    """Return the first report pattern found and its raising frame."""
    for text, frame in failure_evidence(output):
        matches = [pattern for pattern in patterns if pattern in text]
        if not matches:
            continue
        pattern = next(
            (candidate for candidate in matches if is_error_message(candidate)),
            matches[0],
        )
        return pattern, frame
    return None


def _frame_before(
    lines: list[str],
    pattern: str,
    frame_patterns: tuple[re.Pattern[str], ...],
) -> str | None:
    error_index: int | None = None
    for index, line in enumerate(lines):
        if pattern in line:
            error_index = index
    if error_index is None:
        return None
    for line in reversed(lines[:error_index]):
        for frame_pattern in frame_patterns:
            match = frame_pattern.match(line)
            if match is not None:
                return match.group("path")
    return None


def _format_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("base_time must include a timezone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time has no timezone")
    return parsed.astimezone(UTC)


def _json_object(text: str) -> dict[str, object] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return None


def _string_list(
    value: object, *, limit: int = 50, size: int = 1000
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise TypeError("expected a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > size:
            raise TypeError("expected a list of strings")
        if any(ord(char) < 0x20 for char in item):
            raise TypeError("expected a list without control characters")
        items.append(item)
    return tuple(items)


def _extra_map(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or len(value) > 50:
        raise TypeError("expected an extra dependency map")
    result: dict[str, tuple[str, ...]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or len(key) > 128:
            raise TypeError("expected an extra name")
        if not isinstance(raw, list) or len(raw) > 100:
            raise TypeError("expected extra dependency lists")
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str) or len(item) > 1000:
                raise TypeError("expected extra dependency strings")
            if item.startswith("-"):
                raise TypeError("extra dependency must not start with an option")
            if any(ord(char) < 0x20 for char in item):
                raise TypeError(
                    "expected extra dependencies without control characters"
                )
            items.append(item)
        result[key] = tuple(items)
    return result


def _url_list(value: object) -> tuple[UrlDependency, ...]:
    if not isinstance(value, list) or len(value) > 50:
        raise TypeError("expected a list of URLs")
    items: list[UrlDependency] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("expected URL objects")
        requirement = raw.get("requirement")
        url = raw.get("url")
        host_raw = raw.get("host")
        if (
            not isinstance(requirement, str)
            or not isinstance(url, str)
            or not isinstance(host_raw, str)
            or len(requirement) > 500
            or len(url) > 1000
        ):
            raise TypeError("expected URL strings")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise TypeError("expected an http or https URL")
        host = normalize_host(parsed.hostname)
        if host != normalize_host(host_raw):
            raise TypeError("URL host does not match")
        items.append(UrlDependency(requirement=requirement, url=url, host=host))
    return tuple(items)


def _parse_project_info(
    payload: dict[str, object], step: SetupStep, base_time: datetime
) -> ProjectInfo:
    raw_versions = _string_list(payload.get("python_versions"), limit=50, size=16)
    pinned_raw = payload.get("pinned_python")
    if pinned_raw is not None and not isinstance(pinned_raw, str):
        raise TypeError("pinned_python")
    requires_raw = payload.get("requires_python")
    if requires_raw is not None and not isinstance(requires_raw, str):
        raise TypeError("requires_python")
    name_raw = payload.get("project_name")
    if name_raw is not None and not isinstance(name_raw, str):
        raise TypeError("project_name")
    version_raw = payload.get("project_version")
    if version_raw is not None and not isinstance(version_raw, str):
        raise TypeError("project_version")
    uses_scm = payload.get("uses_scm")
    if not isinstance(uses_scm, bool):
        raise TypeError("uses_scm")
    extras = _string_list(payload.get("extras"), limit=50, size=128)
    extra_dependencies = _extra_map(payload.get("extra_dependencies", {}))
    django_raw = payload.get("django_settings")
    if django_raw is not None and (
        not isinstance(django_raw, str)
        or len(django_raw) > 200
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", django_raw) is None
    ):
        raise TypeError("django_settings")
    django_settings = django_raw if isinstance(django_raw, str) else None
    django_suite = payload.get("django_suite", False)
    if not isinstance(django_suite, bool):
        raise TypeError("django_suite")
    matplotlib_build = payload.get("matplotlib_build", False)
    if not isinstance(matplotlib_build, bool):
        raise TypeError("matplotlib_build")
    importlib_mode = payload.get("importlib_mode", False)
    if not isinstance(importlib_mode, bool):
        raise TypeError("importlib_mode")
    git_dependencies = _string_list(payload.get("git_dependencies"), limit=50)
    url_dependencies = _url_list(payload.get("url_dependencies"))
    sources = _string_list(payload.get("sources"), limit=200, size=500)
    pinned = pinned_raw if isinstance(pinned_raw, str) else None
    requires = requires_raw[:500] if isinstance(requires_raw, str) else None
    name = name_raw[:200] if isinstance(name_raw, str) else None
    version = version_raw[:200] if isinstance(version_raw, str) else None
    chosen = _choose_python(
        versions=raw_versions,
        pinned=pinned,
        requires_python=requires,
        base_time=base_time,
    )
    return ProjectInfo(
        python_version=chosen,
        image=PYTHON_IMAGES[chosen],
        requires_python=requires,
        project_name=name,
        project_version=version,
        uses_scm=uses_scm,
        extras=extras,
        extra_dependencies=extra_dependencies,
        django_settings=django_settings,
        django_suite=django_suite,
        matplotlib_build=matplotlib_build,
        importlib_mode=importlib_mode,
        git_dependencies=git_dependencies,
        url_dependencies=url_dependencies,
        sources=sources,
        inspection=step,
    )


def _compare(left: str, right: str) -> int:
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    return (left_parts > right_parts) - (left_parts < right_parts)


def _allows(requires: str | None, version: str) -> bool:
    if not requires:
        return True
    for clause in requires.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.fullmatch(
            r"(~=|===|==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+){0,3})(\.\*)?",
            clause,
        )
        if match is None:
            continue
        operator, raw, _wildcard = match.groups()
        target = ".".join(raw.split(".")[:2])
        if operator in ("==", "==="):
            allowed = _compare(version, target) == 0
        elif operator == "!=":
            allowed = _compare(version, target) != 0
        elif operator == ">=":
            allowed = _compare(version, target) >= 0
        elif operator == ">":
            allowed = _compare(version, target) > 0
        elif operator == "<=":
            allowed = _compare(version, target) <= 0
        elif operator == "<":
            allowed = _compare(version, target) < 0
        else:
            if _compare(version, target) < 0:
                allowed = False
            else:
                parts = raw.split(".")
                if len(parts) <= 2:
                    upper = str(int(parts[0]) + 1) + ".0"
                else:
                    upper = parts[0] + "." + str(int(parts[1]) + 1)
                allowed = _compare(version, upper) < 0
        if not allowed:
            return False
    return True


def _choose_python(
    *,
    versions: Sequence[str],
    pinned: str | None,
    requires_python: str | None,
    base_time: datetime,
) -> str:
    supported = tuple(sorted(PYTHON_IMAGES))
    released = [
        value for value in supported if PYTHON_RELEASES[value] <= base_time.date()
    ]
    if not released:
        raise SetupFailed(
            "unsupported-python",
            "no supported Python was released before the base commit",
        )
    if pinned in released and _allows(requires_python, pinned):
        return pinned
    explicit = [value for value in versions if value in released]
    allowed = [value for value in explicit if _allows(requires_python, value)]
    if allowed:
        return max(allowed, key=lambda item: tuple(int(x) for x in item.split(".")))
    fallback = [value for value in released if _allows(requires_python, value)]
    if fallback:
        return max(fallback, key=lambda item: tuple(int(x) for x in item.split(".")))
    raise SetupFailed(
        "unsupported-python",
        f"no digest-pinned Python image satisfies requires-python {requires_python!r}",
    )


def _step_from_result(
    kind: str, command: Sequence[str], result: CommandResult
) -> SetupStep:
    return SetupStep(
        kind=kind,
        command=tuple(command),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        timed_out=result.timed_out,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
    )


def inspect_project(checkout: Checkout, *, base_time: datetime) -> ProjectInfo:
    """Read project metadata in the trusted fetcher and choose an image."""
    fetcher = checkout.fetcher
    if fetcher.role != "fetch":
        raise ValueError("inspect_project requires the trusted fetcher")
    command = ["python", "-c", _INSPECT_SCRIPT, CHECKOUT]
    try:
        result = fetcher.run(command, timeout=120)
    except TimeLimitExceeded as exc:
        raise SetupFailed("metadata-failed", str(exc)) from exc
    step = _step_from_result("inspect-project", command, result)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SetupFailed(
            "metadata-failed",
            "project metadata inspection failed",
            detail,
        )
    payload = _json_object(result.stdout)
    if payload is None:
        raise SetupFailed(
            "metadata-invalid",
            "project metadata inspection returned no JSON",
            result.stdout + result.stderr,
        )
    try:
        return _parse_project_info(payload, step, base_time)
    except (TypeError, ValueError, SetupFailed) as exc:
        if isinstance(exc, SetupFailed):
            raise
        raise SetupFailed(
            "metadata-invalid", f"project metadata is malformed: {exc}"
        ) from exc


def _base_env(project: ProjectInfo, scm_version: str | None) -> dict[str, str]:
    environment = {
        "PATH": f"/tmp/.local/bin:{VENV}/bin:/usr/local/bin:/usr/bin:/bin",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": "/tmp/uv-cache",
        "UV_LINK_MODE": "copy",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if scm_version:
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = scm_version
    if project.matplotlib_build:
        environment.update(
            {
                "MPLSETUPCFG": MPL_CONFIG,
                "CPATH": f"{MPL_BUILD_TOOLS}/cmeel.prefix/include",
                "LIBRARY_PATH": f"{MPL_BUILD_TOOLS}/cmeel.prefix/lib",
                "LDFLAGS": (f"-Wl,-rpath,{MPL_BUILD_TOOLS}/cmeel.prefix/lib"),
            }
        )
    return environment


def _run_step(
    sandbox: Sandbox,
    steps: list[SetupStep],
    kind: str,
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str = CHECKOUT,
) -> CommandResult:
    try:
        result = sandbox.run(command, cwd=cwd, env=env)
    except TimeLimitExceeded as exc:
        if exc.result is not None:
            steps.append(_step_from_result(kind, command, exc.result))
        raise
    steps.append(_step_from_result(kind, command, result))
    if result.timed_out:
        raise TimeLimitExceeded("the run's time limit has been reached", result)
    return result


def _install_command(
    target: str,
    *,
    base_time: str,
    editable: bool,
    no_build_isolation: bool,
) -> list[str]:
    command = [
        UV_BIN,
        "pip",
        "install",
        "--python",
        f"{VENV}/bin/python",
        "--exclude-newer",
        base_time,
        "--reinstall",
        "--no-cache",
    ]
    if no_build_isolation:
        command.append("--no-build-isolation")
    if editable:
        command.append("--editable")
    command.append(target)
    return command


def _dependencies_failed(result: CommandResult) -> str:
    return result.stderr.strip() or result.stdout.strip() or "no output"


def _looks_like_git(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "git+",
        "git://",
        "hg+",
        "svn+",
        "bzr+",
        "ssh://",
        "failed to clone",
        "could not read from remote",
        "repository not found",
        "git fetch",
    )
    if any(marker in lowered for marker in markers):
        return True
    for line in lowered.splitlines():
        if not any(
            host in line for host in ("github.com", "gitlab.com", "bitbucket.org")
        ):
            continue
        if any(word in line for word in ("clone", "fetch", "repository", "remote")):
            return True
    return False


def _parse_direct_url(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError("an installed package has malformed direct_url.json") from exc
    if not isinstance(parsed, dict):
        raise AuditError("an installed package has malformed direct_url.json")
    return cast(dict[str, object], parsed)


def _distribution_rows(result: CommandResult) -> tuple[Distribution, ...]:
    payload = _json_object(result.stdout)
    if payload is None or not isinstance(payload.get("rows"), list):
        raise AuditError("the installed package list is malformed")
    rows = cast(list[object], payload["rows"])
    if len(rows) > 5000:
        raise AuditError("the installed package list is too large")
    distributions: list[Distribution] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise AuditError("the installed package list is malformed")
        name = raw.get("name")
        version = raw.get("version")
        direct = raw.get("direct_url")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AuditError("the installed package list is malformed")
        if _NAME_PATTERN.fullmatch(name) is None:
            raise AuditError(f"installed package name is invalid: {name[:80]!r}")
        if _VERSION_PATTERN.fullmatch(version) is None:
            raise AuditError(f"installed package version is invalid: {version[:80]!r}")
        if direct is not None and not isinstance(direct, str):
            raise AuditError("the installed package list is malformed")
        distributions.append(
            Distribution(
                name=name, version=version, direct_url=_parse_direct_url(direct)
            )
        )
    return tuple(distributions)


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_name(value: str) -> str:
    if _NAME_PATTERN.fullmatch(value) is None:
        raise AuditError(f"package name is invalid: {value[:80]!r}")
    return value


def _validate_version(value: str) -> str:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise AuditError(f"package version is invalid: {value[:80]!r}")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirect refused",
            headers,
            fp,
        )


_OPENER: Final = urllib.request.build_opener(_NoRedirect())


def _fetch_json(
    url: str,
    *,
    deadline: Deadline,
    transport: Callable[[str, float], bytes] | None,
) -> tuple[dict[str, object], float]:
    started = time.monotonic()
    last_error: str | None = None
    for attempt in range(REGISTRY_ATTEMPTS):
        timeout = deadline.bounded(15)
        try:
            if transport is not None:
                raw = transport(url, timeout)
            else:
                request = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "bugrepro"},
                )
                with _OPENER.open(request, timeout=timeout) as response:
                    raw = cast(bytes, response.read(MAX_REGISTRY_BYTES + 1))
            if len(raw) > MAX_REGISTRY_BYTES:
                raise AuditError("the registry response was too large")
            payload: object = json.loads(raw)
            if not isinstance(payload, dict):
                raise AuditError("the registry response was not a JSON object")
            return cast(dict[str, object], payload), round(
                time.monotonic() - started, 3
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AuditError(f"the registry has no entry for {url}") from exc
            last_error = f"the registry returned HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"the registry could not be reached: {exc}"
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditError("the registry returned malformed JSON") from exc
        if attempt + 1 < REGISTRY_ATTEMPTS:
            delay = min(0.5 * (2**attempt), deadline.remaining())
            if delay <= 0:
                break
            time.sleep(delay)
    raise AuditError(last_error or "the registry lookup failed")


def _upload_time(payload: dict[str, object]) -> datetime:
    files = payload.get("urls")
    if not isinstance(files, list) or not files:
        raise AuditError("the registry entry has no files")
    times: list[datetime] = []
    for raw in files:
        if not isinstance(raw, dict):
            continue
        value = raw.get("upload_time_iso_8601") or raw.get("upload_time")
        if not isinstance(value, str):
            continue
        try:
            times.append(_parse_time(value))
        except ValueError as exc:
            raise AuditError("the registry upload time is invalid") from exc
    if not times:
        raise AuditError("the registry entry has no upload time")
    return min(times)


def audit_installed(
    distributions: Sequence[Distribution],
    base_time: datetime,
    *,
    project_name: str | None = None,
    deadline: Deadline | None = None,
    registry_url: str = PYPI_JSON,
    transport: Callable[[str, float], bytes] | None = None,
) -> tuple[InstalledPackage, ...]:
    """Look up every installed version's first upload time on the registry."""
    base = _format_time(base_time)
    base_dt = _parse_time(base)
    active_deadline = deadline if deadline is not None else Deadline(60)
    parsed_registry = urllib.parse.urlsplit(registry_url)
    if parsed_registry.scheme not in ("http", "https") or not parsed_registry.hostname:
        raise ValueError("registry_url must be an http or https URL")
    if parsed_registry.username or parsed_registry.password:
        raise ValueError("registry_url must not contain credentials")
    base_url = registry_url.rstrip("/")
    project = _normalize_name(project_name) if project_name else None
    results: list[InstalledPackage] = []
    for distribution in distributions:
        name = _validate_name(distribution.name)
        version = _validate_version(distribution.version)
        if project is not None and _normalize_name(name) == project:
            results.append(
                InstalledPackage(
                    name=name,
                    version=version,
                    upload_time=None,
                    source="project",
                )
            )
            continue
        direct = distribution.direct_url
        if direct is not None:
            if "dir_info" in direct:
                results.append(
                    InstalledPackage(
                        name=name,
                        version=version,
                        upload_time=None,
                        source="local",
                    )
                )
                continue
            if "vcs_info" in direct:
                raise AuditError(
                    f"installed package {name} came from a git dependency",
                    reason="git-dependency",
                )
            raw_url = direct.get("url")
            if isinstance(raw_url, str):
                parsed = urllib.parse.urlsplit(raw_url)
                if parsed.scheme == "file":
                    results.append(
                        InstalledPackage(
                            name=name,
                            version=version,
                            upload_time=None,
                            source="local",
                        )
                    )
                    continue
                if parsed.scheme in ("http", "https") and parsed.hostname:
                    host = normalize_host(parsed.hostname)
                    if host in REGISTRIES:
                        results.append(
                            InstalledPackage(
                                name=name,
                                version=version,
                                upload_time=None,
                                source="url",
                                host=host,
                                unbounded=True,
                            )
                        )
                        continue
                    raise AuditError(
                        f"installed package {name} came from a host that is not "
                        f"allowlisted: {host}"
                    )
        usable_name = urllib.parse.quote(name, safe="")
        usable_version = urllib.parse.quote(version, safe="")
        url = f"{base_url}/{usable_name}/{usable_version}/json"
        payload, _ = _fetch_json(url, deadline=active_deadline, transport=transport)
        uploaded = _upload_time(payload)
        results.append(
            InstalledPackage(
                name=name,
                version=version,
                upload_time=_format_time(uploaded),
                source="pypi",
                newer=uploaded > base_dt,
            )
        )
    return tuple(results)


def _version_key(version: str) -> tuple[int, ...]:
    match = re.match(r"^[vV]?(\d+(?:\.\d+)*)", version)
    if match is None:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def latest_release_before(
    project_name: str,
    base_time: datetime,
    *,
    deadline: Deadline | None = None,
    registry_url: str = PYPI_JSON,
    transport: Callable[[str, float], bytes] | None = None,
) -> str:
    """Return the highest project release uploaded at or before the base time."""
    name = urllib.parse.quote(_validate_name(project_name), safe="")
    base_dt = _parse_time(_format_time(base_time))
    active_deadline = deadline if deadline is not None else Deadline(60)
    payload, _ = _fetch_json(
        f"{registry_url.rstrip('/')}/{name}/json",
        deadline=active_deadline,
        transport=transport,
    )
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        raise AuditError("the registry project has no releases")
    best: tuple[tuple[int, ...], str] | None = None
    for version, files in releases.items():
        if not isinstance(version, str) or not isinstance(files, list):
            continue
        try:
            _validate_version(version)
        except AuditError:
            continue
        if re.search(r"(?:a|b|rc|dev)\d*$", version, re.IGNORECASE):
            continue
        times: list[datetime] = []
        for raw in files:
            if not isinstance(raw, dict):
                continue
            value = raw.get("upload_time_iso_8601") or raw.get("upload_time")
            if not isinstance(value, str):
                continue
            try:
                times.append(_parse_time(value))
            except ValueError:
                continue
        if not times or min(times) > base_dt:
            continue
        key = _version_key(version)
        if best is None or key > best[0]:
            best = (key, version)
    if best is None:
        raise AuditError("no project release was uploaded before the base time")
    return best[1]


def _prepare_environment(
    sandbox: Sandbox,
    steps: list[SetupStep],
    project: ProjectInfo,
    base_time: str,
    deadline: Deadline,
) -> tuple[dict[str, str], str | None]:
    environment = _base_env(project, None)
    result = _run_step(
        sandbox,
        steps,
        "tooling",
        [
            "python",
            "-m",
            "pip",
            "install",
            "--user",
            "--no-cache-dir",
            f"uv=={UV_VERSION}",
        ],
        env=environment,
    )
    if not result.ok:
        raise SetupFailed(
            "tooling-failed", "uv could not be installed", _dependencies_failed(result)
        )
    result = _run_step(
        sandbox,
        steps,
        "venv",
        ["python", "-m", "venv", "--without-pip", VENV],
        env=environment,
    )
    if not result.ok:
        raise SetupFailed(
            "tooling-failed",
            "the venv could not be created",
            _dependencies_failed(result),
        )
    scm_version = project.project_version
    if project.uses_scm and not scm_version and project.project_name:
        try:
            scm_version = latest_release_before(
                project.project_name, _parse_time(base_time), deadline=deadline
            )
        except AuditError as exc:
            raise SetupFailed("scm-version", str(exc)) from exc
    environment = _base_env(project, scm_version)
    return environment, scm_version


def _prepare_matplotlib_build(
    sandbox: Sandbox,
    steps: list[SetupStep],
    environment: dict[str, str],
) -> None:
    result = _run_step(
        sandbox,
        steps,
        "matplotlib-build-tool",
        [
            "/usr/local/bin/python",
            "-m",
            "pip",
            "install",
            "--target",
            MPL_BUILD_TOOLS,
            "--no-deps",
            "--no-cache-dir",
            f"cmeel-qhull=={CMEEL_QHULL_VERSION}",
        ],
        env=environment,
    )
    if not result.ok:
        raise SetupFailed(
            "tooling-failed",
            "the qhull build tool could not be installed",
            _dependencies_failed(result),
        )
    result = _run_step(
        sandbox,
        steps,
        "matplotlib-build-config",
        [
            "python",
            "-c",
            (
                "from pathlib import Path; "
                f"Path({MPL_CONFIG!r}).write_text("
                "'[libs]\\nsystem_freetype = True\\nsystem_qhull = True\\n')"
            ),
        ],
        env=environment,
    )
    if not result.ok:
        raise SetupFailed(
            "tooling-failed",
            "the matplotlib build configuration could not be written",
            _dependencies_failed(result),
        )


def _install_project(
    sandbox: Sandbox,
    steps: list[SetupStep],
    project: ProjectInfo,
    environment: dict[str, str],
    base_time: str,
) -> CommandResult:
    attempts = [
        _install_command(
            CHECKOUT, base_time=base_time, editable=False, no_build_isolation=False
        ),
        _install_command(
            CHECKOUT, base_time=base_time, editable=True, no_build_isolation=False
        ),
    ]
    if project.importlib_mode:
        attempts.reverse()
    last: CommandResult | None = None
    for command in attempts:
        last = _run_step(sandbox, steps, "install", command, env=environment)
        if last.ok:
            return last
        if _looks_like_git(last.stdout + last.stderr):
            raise SetupFailed(
                "git-dependency",
                "a git dependency cannot be fetched during install",
                _dependencies_failed(last),
            )
    if last is None:
        raise SetupFailed("resolver-failed", "no install command ran")
    tooling_env = dict(environment)
    tooling = _run_step(
        sandbox,
        steps,
        "build-tools",
        ["python", "-m", "venv", BUILD_TOOLS],
        env=environment,
    )
    if tooling.ok:
        tooling = _run_step(
            sandbox,
            steps,
            "build-tools",
            [
                f"{BUILD_TOOLS}/bin/python",
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "setuptools>=64",
                "wheel",
            ],
            env=environment,
        )
    if tooling.ok:
        site = f"{BUILD_TOOLS}/lib/python{project.python_version}/site-packages"
        tooling_env["PYTHONPATH"] = site
        last = _run_step(
            sandbox,
            steps,
            "install",
            _install_command(
                CHECKOUT,
                base_time=base_time,
                editable=True,
                no_build_isolation=True,
            ),
            env=tooling_env,
        )
        if last.ok:
            return last
        if _looks_like_git(last.stdout + last.stderr):
            raise SetupFailed(
                "git-dependency",
                "a git dependency cannot be fetched during install",
                _dependencies_failed(last),
            )
    raise SetupFailed(
        "resolver-failed",
        "the resolver could not build the project within the date bound",
        _dependencies_failed(last),
    )


def _collection_extra(name: str) -> bool:
    lowered = name.lower()
    return lowered in {
        "test",
        "tests",
        "testing",
        "dev",
        "develop",
    } or lowered.startswith(("test-", "tests-", "testing-"))


def _install_extras(
    sandbox: Sandbox,
    steps: list[SetupStep],
    project: ProjectInfo,
    environment: dict[str, str],
    base_time: str,
    notes: list[str],
) -> None:
    for extra in project.extras:
        if not _collection_extra(extra):
            continue
        command = _install_command(
            f"{CHECKOUT}[{extra}]",
            base_time=base_time,
            editable=False,
            no_build_isolation=False,
        )
        result = _run_step(
            sandbox, steps, f"install-extra:{extra}", command, env=environment
        )
        if result.ok:
            continue
        if _looks_like_git(result.stdout + result.stderr):
            raise SetupFailed(
                "git-dependency",
                "a git dependency cannot be fetched during install",
                _dependencies_failed(result),
            )
        notes.append(f"optional extra {extra} failed to install")
        for requirement in project.extra_dependencies.get(extra, ()):
            if requirement.startswith("-"):
                raise SetupFailed(
                    "metadata-invalid",
                    "an extra dependency starts with an option",
                    requirement,
                )
            if _looks_like_git(requirement):
                raise SetupFailed(
                    "git-dependency",
                    "a git dependency cannot be fetched during install",
                    requirement,
                )
            dependency_command = _install_command(
                requirement,
                base_time=base_time,
                editable=False,
                no_build_isolation=False,
            )
            dependency = _run_step(
                sandbox,
                steps,
                f"install-extra-dependency:{extra}",
                dependency_command,
                env=environment,
            )
            if not dependency.ok:
                if _looks_like_git(dependency.stdout + dependency.stderr):
                    raise SetupFailed(
                        "git-dependency",
                        "a git dependency cannot be fetched during install",
                        _dependencies_failed(dependency),
                    )
                notes.append(
                    f"optional dependency {requirement[:80]!r} failed to install"
                )


def _ensure_test_runner(
    sandbox: Sandbox,
    steps: list[SetupStep],
    project: ProjectInfo,
    environment: dict[str, str],
    base_time: str,
) -> None:
    if project.django_suite:
        return
    check = _run_step(
        sandbox,
        steps,
        "test-runner",
        [f"{VENV}/bin/python", "-m", "pytest", "--version"],
        env=environment,
    )
    if not check.ok:
        command = _install_command(
            "pytest", base_time=base_time, editable=False, no_build_isolation=False
        )
        result = _run_step(
            sandbox, steps, "install-test-runner", command, env=environment
        )
        if not result.ok:
            raise SetupFailed(
                "test-runner-failed",
                "pytest could not be installed for collection",
                _dependencies_failed(result),
            )
    if project.django_settings is not None:
        command = _install_command(
            "pytest-django",
            base_time=base_time,
            editable=False,
            no_build_isolation=False,
        )
        result = _run_step(
            sandbox, steps, "install-test-runner", command, env=environment
        )
        if not result.ok:
            raise SetupFailed(
                "test-runner-failed",
                "pytest-django could not be installed for collection",
                _dependencies_failed(result),
            )


def _collection_env(
    environment: dict[str, str], project: ProjectInfo
) -> dict[str, str]:
    if project.django_settings is None:
        return environment
    return {**environment, "DJANGO_SETTINGS_MODULE": project.django_settings}


def _list_installed(
    sandbox: Sandbox,
    steps: list[SetupStep],
    environment: dict[str, str],
) -> tuple[Distribution, ...]:
    command = [f"{VENV}/bin/python", "-c", _LIST_SCRIPT]
    result = _run_step(sandbox, steps, "list-installed", command, env=environment)
    if not result.ok:
        raise AuditError(
            "the installed package list could not be read",
        )
    return _distribution_rows(result)


def _collection_command(project: ProjectInfo) -> list[str]:
    command = [
        f"{VENV}/bin/python",
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if project.importlib_mode:
        command.extend(["--import-mode=importlib"])
    command.extend(["--ignore", f"{CHECKOUT}/build", "--ignore", f"{CHECKOUT}/dist"])
    return command


def _collect(
    sandbox: Sandbox,
    steps: list[SetupStep],
    project: ProjectInfo,
    environment: dict[str, str],
) -> tuple[CommandResult, int | None]:
    if project.django_suite:
        command = [
            f"{VENV}/bin/python",
            "-c",
            _DJANGO_COLLECT_SCRIPT,
            CHECKOUT,
        ]
    else:
        command = _collection_command(project)
    result = _run_step(sandbox, steps, "collect", command, env=environment)
    if not result.ok:
        return result, None
    try:
        count = collect_count(result.stdout + result.stderr)
    except ValueError:
        return result, None
    return result, count


def _command_text(command: Sequence[str]) -> str:
    return " ".join(command)


def _model_prompt(
    project: ProjectInfo,
    base_time: str,
    failure: CommandResult,
) -> str:
    output = (failure.stdout + failure.stderr)[-MAX_STEP_OUTPUT_CHARS:]
    return (
        "A Python project's deterministic setup could not collect its tests.\n"
        'Return only JSON of the form {"commands": [["uv", ...], ...]} with at '
        f"most {MODEL_COMMANDS_PER_TURN} commands.\n"
        f"The dependency cutoff is exactly {base_time}. Do not put date flags in "
        "commands; the harness adds --exclude-newer and --reinstall to uv pip "
        "install.\n"
        f"Allowed command programs: uv, python, python3, pytest. Do not use "
        f"python -c. The checkout is {CHECKOUT} and the venv is {VENV}.\n"
        "The only allowed package hosts are pypi.org and files.pythonhosted.org; "
        "git dependencies cannot be fetched.\n"
        f"Project: {project.project_name or 'unknown'}, Python "
        f"{project.python_version}, extras {list(project.extras)}.\n"
        f"Collection command: {_command_text(_collection_command(project))}\n"
        f"Collection output:\n{output}\n"
    )


def _parse_model_commands(text: str) -> list[list[str]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[^\n]*\n", "", stripped)
        stripped = re.sub(r"\n```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the model returned no JSON object")
    parsed: object = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("the model returned no command object")
    raw_commands = cast(dict[str, object], parsed).get("commands")
    if not isinstance(raw_commands, list):
        raise TypeError("the model returned no commands")
    if len(raw_commands) > MODEL_COMMANDS_PER_TURN:
        raise ValueError("the model returned too many commands")
    commands: list[list[str]] = []
    for raw in raw_commands:
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("a model command is not a list of strings")
        commands.append(cast(list[str], raw))
    return commands


def _validate_model_command(command: Sequence[str], base_time: str) -> list[str]:
    if not command or len(command) > 30:
        raise ValueError("a model command has the wrong length")
    for argument in command:
        if not argument or len(argument) > MAX_MODEL_ARG:
            raise ValueError("a model command argument has the wrong length")
        if any(char in _MODEL_REJECTED_CHARS or ord(char) < 0x20 for char in argument):
            raise ValueError("a model command contains a shell character")
    if command[0] not in _MODEL_ALLOWED:
        raise ValueError(f"program {command[0]!r} is not allowed")
    joined = "\0".join(command)
    if any(marker in joined.lower() for marker in _MODEL_REJECTED_PARTS):
        raise ValueError("a model command reaches a git host")
    if ".." in joined:
        raise ValueError("a model command contains a parent path")
    for argument in command:
        if argument.startswith("/") and not any(
            argument == prefix or argument.startswith(prefix + "/")
            for prefix in _MODEL_ALLOWED_PREFIXES
        ):
            raise ValueError("a model command writes outside the workspace")
        for match in re.finditer(r"https?://[^\s]+", argument):
            parsed = urllib.parse.urlsplit(match.group(0))
            if not parsed.hostname or normalize_host(parsed.hostname) not in REGISTRIES:
                raise ValueError("a model command reaches a non-allowlisted URL")
    if command[0] in ("python", "python3"):
        if "pip" in command:
            raise ValueError("use uv pip install, not python -m pip install")
        if "-c" in command:
            raise ValueError("python -c is not allowed during setup")
    if command[0] == "uv":
        if len(command) < 3 or command[1:3] != ["pip", "install"]:
            raise ValueError("only uv pip install is allowed")
        for index, argument in enumerate(command):
            if argument in ("--index-url", "--extra-index-url", "--find-links", "-i"):
                raise ValueError("a model command changes the package index")
            if argument.startswith(
                ("--index-url=", "--extra-index-url=", "--find-links=")
            ):
                raise ValueError("a model command changes the package index")
            if argument == "--no-index":
                raise ValueError("a model command changes the package index")
            if argument == "--uploaded-prior-to":
                raise ValueError("uv commands use --exclude-newer")
            if argument == "--exclude-newer" and (
                index + 1 >= len(command) or command[index + 1] != base_time
            ):
                raise ValueError("a model command changes the date bound")
            if (
                argument.startswith("--exclude-newer=")
                and argument.removeprefix("--exclude-newer=") != base_time
            ):
                raise ValueError("a model command changes the date bound")
        if "--python" not in command:
            command = [*command[:3], "--python", f"{VENV}/bin/python", *command[3:]]
        if "--exclude-newer" not in command and not any(
            argument.startswith("--exclude-newer=") for argument in command
        ):
            command = [*command[:3], "--exclude-newer", base_time, *command[3:]]
        if "--reinstall" not in command:
            command = [*command[:3], "--reinstall", *command[3:]]
    return list(command)


def _run_model_turn(
    sandbox: Sandbox,
    steps: list[SetupStep],
    model: ModelClient,
    project: ProjectInfo,
    base_time: str,
    failure: CommandResult,
    environment: dict[str, str],
) -> bool:
    prompt = _model_prompt(project, base_time, failure)
    try:
        result = model.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=1024,
            deadline=sandbox.deadline,
        )
    except TimeLimitExceeded:
        raise
    except ModelError as exc:
        raise SetupFailed("model-error", str(exc)) from exc
    if result.outcome is Outcome.STOPPED_AT_COST_CAP:
        raise SetupFailed(
            "stopped-at-cost-cap",
            "the model call stopped at the cost cap",
        )
    if result.outcome is Outcome.ERROR or result.text is None:
        raise SetupFailed(
            "model-error", result.error or "the model returned no setup commands"
        )
    try:
        commands = _parse_model_commands(result.text)
        validated = [
            _validate_model_command(command, base_time) for command in commands
        ]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SetupFailed("model-command-invalid", str(exc)) from exc
    for command in validated:
        _run_step(sandbox, steps, "model-command", command, env=environment)
    return bool(validated)


def _finish(
    *,
    outcome: str,
    reason: str | None,
    project: ProjectInfo,
    base_time: str,
    steps: Sequence[SetupStep],
    installed: Sequence[InstalledPackage] = (),
    unbounded: Sequence[UnboundedDependency] = (),
    count: int | None = None,
    notes: Sequence[str] = (),
    error: str | None = None,
) -> SetupResult:
    return SetupResult(
        outcome=outcome,
        reason=reason,
        image=project.image,
        python_version=project.python_version,
        base_time=base_time,
        steps=tuple(steps),
        installed=tuple(installed),
        unbounded=tuple(unbounded),
        collect_count=count,
        notes=tuple(notes),
        error=error,
    )


def build_environment(
    sandbox: Sandbox,
    budget: Budget,
    *,
    base_time: datetime,
    project: ProjectInfo,
    model: ModelClient | None = None,
) -> SetupResult:
    """Build a Python environment and prove collection works."""
    if model is not None and model.budget is not budget:
        raise ValueError("model must use the run's budget")
    base = _format_time(base_time)
    if project.image not in PYTHON_IMAGES.values():
        raise ValueError("project.image is not a digest-pinned Python image")
    if PYTHON_IMAGES.get(project.python_version) != project.image:
        raise ValueError("project.python_version does not match project.image")
    steps: list[SetupStep] = []
    if project.inspection is not None:
        steps.append(project.inspection)
    if PYTHON_RELEASES[project.python_version] > _parse_time(base).date():
        return _finish(
            outcome=OUTCOME_FAILED,
            reason="unsupported-python",
            project=project,
            base_time=base,
            steps=steps,
            error=(
                f"Python {project.python_version} was not released by the "
                "base commit time"
            ),
        )
    unbounded = [
        UnboundedDependency(kind="url", requirement=item.requirement, host=item.host)
        for item in project.url_dependencies
        if item.host in REGISTRIES
    ]
    if project.git_dependencies:
        return _finish(
            outcome=OUTCOME_FAILED,
            reason="git-dependency",
            project=project,
            base_time=base,
            steps=steps,
            unbounded=unbounded,
            error=(
                "git dependencies cannot be fetched during install: "
                + "; ".join(project.git_dependencies)
            ),
        )
    notes: list[str] = []
    installed: tuple[InstalledPackage, ...] = ()
    try:
        sandbox.allow(Phase.INSTALL, REGISTRIES)
        environment, scm_version = _prepare_environment(
            sandbox, steps, project, base, sandbox.deadline
        )
        _ = scm_version
        if project.matplotlib_build:
            _prepare_matplotlib_build(sandbox, steps, environment)
        _install_project(sandbox, steps, project, environment, base)
        _install_extras(sandbox, steps, project, environment, base, notes)
        _ensure_test_runner(sandbox, steps, project, environment, base)
        collection_env = _collection_env(environment, project)
        distributions = _list_installed(sandbox, steps, environment)
        audited = audit_installed(
            distributions,
            _parse_time(base),
            project_name=project.project_name,
            deadline=sandbox.deadline,
        )
        installed = audited
        for item in audited:
            if item.unbounded:
                unbounded.append(
                    UnboundedDependency(
                        kind="url",
                        requirement=f"{item.name}=={item.version}",
                        host=item.host,
                    )
                )
        collection, count = _collect(sandbox, steps, project, collection_env)
        if count is not None:
            return _finish(
                outcome=OUTCOME_SUCCEEDED,
                reason=None,
                project=project,
                base_time=base,
                steps=steps,
                installed=installed,
                unbounded=unbounded,
                count=count,
                notes=notes,
            )
        if model is None:
            return _finish(
                outcome=OUTCOME_FAILED,
                reason="collection-failed",
                project=project,
                base_time=base,
                steps=steps,
                installed=installed,
                unbounded=unbounded,
                notes=notes,
                error=(
                    "the project's tests could not be collected: "
                    + _dependencies_failed(collection)
                ),
            )
        last_collection = collection
        for _turn in range(MODEL_TURNS):
            progressed = _run_model_turn(
                sandbox,
                steps,
                model,
                project,
                base,
                last_collection,
                collection_env,
            )
            if not progressed:
                break
            last_collection, count = _collect(sandbox, steps, project, collection_env)
            if count is not None:
                return _finish(
                    outcome=OUTCOME_SUCCEEDED,
                    reason=None,
                    project=project,
                    base_time=base,
                    steps=steps,
                    installed=installed,
                    unbounded=unbounded,
                    count=count,
                    notes=notes,
                )
        return _finish(
            outcome=OUTCOME_FAILED,
            reason="collection-failed",
            project=project,
            base_time=base,
            steps=steps,
            installed=installed,
            unbounded=unbounded,
            notes=notes,
            error=(
                "the project's tests still could not be collected: "
                + _dependencies_failed(last_collection)
            ),
        )
    except TimeLimitExceeded as exc:
        return _finish(
            outcome=OUTCOME_STOPPED_AT_TIME_LIMIT,
            reason="stopped-at-time-limit",
            project=project,
            base_time=base,
            steps=steps,
            installed=installed,
            unbounded=unbounded,
            notes=notes,
            error=str(exc),
        )
    except SetupFailed as exc:
        if exc.reason == "stopped-at-cost-cap":
            outcome = OUTCOME_STOPPED_AT_COST_CAP
        else:
            outcome = OUTCOME_FAILED
        return _finish(
            outcome=outcome,
            reason=exc.reason,
            project=project,
            base_time=base,
            steps=steps,
            installed=installed,
            unbounded=unbounded,
            notes=notes,
            error=str(exc),
        )
    except AuditError as exc:
        return _finish(
            outcome=OUTCOME_FAILED,
            reason=exc.reason,
            project=project,
            base_time=base,
            steps=steps,
            installed=installed,
            unbounded=unbounded,
            notes=notes,
            error=str(exc),
        )
    except SandboxError as exc:
        return _finish(
            outcome=OUTCOME_FAILED,
            reason="sandbox-error",
            project=project,
            base_time=base,
            steps=steps,
            installed=installed,
            unbounded=unbounded,
            notes=notes,
            error=str(exc),
        )
