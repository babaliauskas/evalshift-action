#!/usr/bin/env python3
"""Runtime helper for the EvalShift GitHub Action.

The composite action handles Python setup and package installation. This helper
runs the installed CLI, queries hosted EvalShift for a baseline diff, writes
action outputs, and updates GitHub PR affordances.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

COMMENT_MARKER = "<!-- evalshift:comment -->"
STATUS_CONTEXT = "evalshift/regression"
DEFAULT_HOST = "https://api.evalshift.dev"

FAIL_ON_MODES = ("never", "regression", "any-slice-regression", "policy")
DEFAULT_FAIL_ON = "policy"
# Used when neither 'suite' nor 'suite-name' is set -- the conventional filename a
# single-suite project keeps at its root.
DEFAULT_SUITE_PATH = "golden.jsonl"
# `--suite-name` landed in this CLI release. Older pins know only `--suite <path>`, and
# would fail on an unknown option rather than on anything the reader could act on.
MIN_SUITE_NAME_VERSION = (0, 14, 0)

# A run has two ids and they are not interchangeable. The local one names the directory under
# `.evalshift/runs` and is what `evalshift push` takes as its argument; the server mints its
# own at `POST /runs` and that is the only value `/runs/{id}` routes accept. The CLI prints the
# hosted run URL and nothing else, so the server id reaches the action as the path segment
# behind `/runs/` — a canonical UUID, matched strictly so a truncated or unexpected URL is
# caught here rather than as a puzzling 404 further down.
SERVER_RUN_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Where in a hosted run URL's path that id lives. The server builds exactly one shape —
# `{web_app_url}/app/{org_slug}/{project_slug}/runs/{id}` — so the whole shape is anchored, not
# just the `/runs/` marker. Anchoring only the marker took the LEFTMOST `/runs/<36 chars>`,
# which an org slugged `runs` sitting in front of a UUID-shaped project slug turns into the
# project slug: a valid-looking wrong id, and a wrong id fails silently (404 on policy-check,
# read as "no stored policy decision", degraded past). Slugs really can collide this way —
# the server's `slugify` maps every non-alphanumeric run to `-`, so a UUID-shaped slug is
# reachable from an ordinary-looking project name.
#
# Anchored, not rooted: no `^`, because `web_app_url` is a plain string setting that may carry
# a base path (`https://example.com/tools`). The capture is deliberately loose —
# `SERVER_RUN_ID_PATTERN` above is what decides whether it is a real id.
RUN_URL_ID_PATTERN = re.compile(r"/app/[^/]+/[^/]+/runs/([0-9a-fA-F-]{36})(?:[/?#]|$)")

# The CLI prints through Rich, which folds output at the console width — 80 columns whenever
# stdout is a pipe, which it always is under `run_command`. A hosted run URL runs past 80
# characters, and half a URL is both an unusable link in the PR comment and an unusable run id.
# Rich reads `COLUMNS` when it cannot measure a terminal, so the action names a width no URL
# will reach.
CLI_CONSOLE_COLUMNS = "512"

# What `policy` degrades to when hosted EvalShift cannot supply a decision. Never `never`:
# an unreachable policy endpoint must not be a way to merge a regression unnoticed.
POLICY_FALLBACK_MODE = "regression"

# The server's status vocabulary is a closed set of four: `pass`, `conditional_pass`, `fail`,
# `inconclusive`. Three of them are a decision the action can act on; `inconclusive` is not.
# Anything outside the set is something a future server grew and this action has never seen —
# handled as undecided further down, so an action pinned in a workflow keeps behaving.
POLICY_DECIDED_STATUSES = frozenset({"pass", "conditional_pass", "fail"})

# Decided, passing, and still not clean. `conditional_pass` means every budget held and nothing
# critical or high regressed, but something milder did — medium/low regressions, or comparisons
# that scored zero pairs. It deliberately does not fail the gate; it does ask for a human look,
# and the `reason` is where the server says what to look at.
POLICY_CAVEATED_STATUSES = frozenset({"conditional_pass"})

# The `policy_source` the server reports when the run was pushed carrying no migration policy.
# It answers `inconclusive` for that, exactly as it does for a policy that weighed the run and
# could not decide -- so the source is the only thing separating "nothing gates this PR" from
# "the gate reached no verdict", and the two want opposite words: a configuration gap with a
# one-line fix, versus a real answer about this run.
POLICY_SOURCE_NONE = "none"

NO_POLICY_SUMMARY = (
    "the gate is off — no migration policy was pushed with this run; "
    "add migration_policy to evalshift.yaml"
)

# What `require-policy: true` turns that sentence into. Separate wording because "the gate is
# off" describes a job that merged, and this one describes the job it just blocked.
NO_POLICY_REQUIRED_SUMMARY = (
    "no migration policy was pushed with this run and require-policy is set; "
    "add migration_policy to evalshift.yaml"
)

# GitHub scrapes workflow commands off the step's STDOUT only. `fetch_policy_check`'s fallback
# warning goes to stderr because it is log text about one flaky request; this one has to reach
# the job summary and the PR's Files view, because an ungated repository is invisible otherwise
# -- every check is green and nothing says why.
NO_POLICY_ANNOTATION = (
    "::warning title=EvalShift policy gate::no migration policy was pushed with this run, "
    "so nothing gates this PR; add migration_policy to evalshift.yaml and push again"
)

# A PR comment is not a dashboard. A policy with a per-slice budget for fifty slices would
# otherwise bury the diff under its own table.
MAX_BUDGET_ROWS = 12
MAX_BLOCKING_ROWS = 10
MAX_SLICE_ROWS = 5

# One EvalShift run per job. A matrix asks once per job, and the server counts the
# in-flight runs itself, so declaring anything larger here would be a guess.
PREFLIGHT_PARALLELISM = 1

# The plan preflight: one call, addressed by project slug and authorized by `run:create`, the
# permission `evalshift push` already needs. A server older than the route answers 405 --
# `/runs/{run_id}` matches the path but not the method -- which the action reads as "cannot
# ask" and runs anyway. There is deliberately no fallback to the old project-id route: it sat
# behind `GET /orgs/{org}/projects`, which the documented CI key cannot read.
PREFLIGHT_PATH = "/runs/preflight"

# `project: org/project` at the top level of evalshift.yaml. Read with a regex rather than a
# YAML parser because the action ships with no dependencies, and the CLI constrains the key to
# exactly this shape. Anything this misses only skips the preflight; the upload is still gated.
PROJECT_KEY_PATTERN = re.compile(
    r"""^project:\s*["']?([a-z0-9-]+)/([a-z0-9-]+)["']?\s*(?:\#.*)?$"""
)

# Hosted EvalShift answers an authorization failure with `Permission denied: <key>`,
# where the key comes from its permission catalog (run:create, run:read, ...).
PERMISSION_PATTERN = re.compile(r"Permission denied: ([a-z_]+:[a-z_]+)")
KEY_ADVICE = (
    "Mint a service-account key with the scopes this workflow needs "
    "(EvalShift web app -> Settings -> API tokens -> Service accounts), store it as an "
    "encrypted repository or environment secret, and point the action's 'token' input at it. "
    "A personal token is not a CI credential -- it dies with the person who created it."
)

RequestFn = Callable[[str, str, dict[str, str], bytes | None], Any]
RunnerFn = Callable[[list[str], Path, dict[str, str]], "CommandResult"]


@dataclass(frozen=True)
class ActionConfig:
    token: str
    host: str
    config: str
    suite: str
    evalshift_version: str
    # The `suites:` key, when the suite was selected by name. Empty means `suite` (a path)
    # selects it instead; exactly one of the two is ever set.
    suite_name: str
    fail_on: str
    branch: str
    base_branch: str
    create_project: bool
    comment: bool
    github_token: str
    # Opt-in: fail the job when the run carried no migration policy. Off by default, because a
    # repository that has never pushed a policy would otherwise go red on upgrading the action
    # -- the annotation says the same thing without blocking anyone's merge.
    require_policy: bool = False
    # Asserted by the workflow, not verified by EvalShift. Defaults to public: the server
    # records the first `true` permanently, so guessing `true` would be the costly guess.
    repo_private: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ActionConfig:
        source: Mapping[str, str] = env if env is not None else os.environ
        token = _input(source, "TOKEN")
        if not token:
            raise ActionError("input 'token' is required")
        evalshift_version = _input(source, "EVALSHIFT_VERSION")
        if not evalshift_version:
            # action.yml always passes its default through, so an empty value means the
            # script was run outside the action. The pin lives only in action.yml.
            raise ActionError("input 'evalshift-version' is required")
        fail_on = _input(source, "FAIL_ON", DEFAULT_FAIL_ON)
        if fail_on not in FAIL_ON_MODES:
            raise ActionError(f"input 'fail-on' must be one of: {', '.join(FAIL_ON_MODES)}")
        suite, suite_name = _suite_selection(
            _input(source, "SUITE", ""),
            _input(source, "SUITE_NAME", ""),
            evalshift_version,
        )
        return cls(
            token=token,
            host=_input(source, "HOST", DEFAULT_HOST).rstrip("/"),
            config=_input(source, "CONFIG", "evalshift.yaml"),
            suite=suite,
            suite_name=suite_name,
            evalshift_version=evalshift_version,
            fail_on=fail_on,
            require_policy=_bool_input(source, "REQUIRE_POLICY", False),
            branch=_input(source, "BRANCH", ""),
            base_branch=_input(source, "BASE_BRANCH", ""),
            create_project=_bool_input(source, "CREATE_PROJECT", True),
            comment=_bool_input(source, "COMMENT", True),
            github_token=_input(source, "GITHUB_TOKEN", ""),
            repo_private=_bool_input(source, "REPO_PRIVATE", False),
        )


@dataclass(frozen=True)
class GitHubContext:
    event_name: str
    repository: str
    sha: str
    branch: str
    base_branch: str
    pull_number: int | None
    is_pull_request: bool


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    returncode: int


@dataclass(frozen=True)
class EvalShiftRunResult:
    """One pushed run, as hosted EvalShift knows it.

    ``run_id`` is the **server-minted** id read back out of ``run_url`` — the value every
    ``/runs/{id}`` route takes and the one the action reports as its ``run_id`` output. It is
    deliberately not the local run directory name, which addresses nothing beyond this
    machine's ``.evalshift/runs``.
    """

    run_id: str
    run_url: str


@dataclass(frozen=True)
class GatingResult:
    """The gate's decision, plus everything the PR comment needs to justify it.

    ``conclusion`` stays one of GitHub's commit-status states (``success`` / ``failure``) — it
    is sent to the statuses API verbatim. When the governed policy declines to decide, the job
    does not fail, and ``summary`` is where that is said out loud.
    """

    conclusion: str
    should_fail: bool
    regression_count: int
    top_slice_regressions: list[dict[str, Any]]
    # `fail-on` as configured. Kept even on the fallback path, so the comment can explain that
    # `policy` was asked for and something else was used.
    mode: str = "regression"
    # One plain sentence about how the gate reached its decision. Empty in the diff-only modes,
    # where the conclusion and the regression count already say everything there is to say.
    summary: str = ""
    policy_status: str = ""
    policy_verdict: str = ""
    policy_reason: str = ""
    policy_source: str = ""
    budgets: list[dict[str, Any]] = field(default_factory=list)
    blocking_regressions: list[dict[str, Any]] = field(default_factory=list)
    policy_unavailable_reason: str = ""

    @property
    def policy_decided(self) -> bool:
        """Whether the policy returned a status this action knows how to act on."""
        return self.policy_status in POLICY_DECIDED_STATUSES

    @property
    def policy_ungated(self) -> bool:
        """Whether this run carried no policy at all -- an absent gate, not an undecided one."""
        return self.policy_status == "inconclusive" and self.policy_source == POLICY_SOURCE_NONE

    @property
    def policy_caveated(self) -> bool:
        """Whether the policy passed the run but attached something worth reading first."""
        return self.policy_status in POLICY_CAVEATED_STATUSES


class ActionError(Exception):
    """Raised for action-level user or runtime failures."""


class PreflightDenied(ActionError):
    """402 from the CI preflight: this job is not covered by the organization's plan.

    ``details`` is the server's whole upgrade prompt — feature, tier, limit, used, reset date
    and upgrade URL. The action never decides what a plan covers; it renders what it was told.
    """

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class HostedClient:
    def __init__(
        self,
        host: str,
        token: str,
        *,
        request: RequestFn | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self._request = request or http_request

    def baseline_compatible(self, run_id: str, branch: str) -> dict[str, Any]:
        query = urlencode({"branch": branch})
        path = f"/runs/{quote(run_id)}/baseline-compatible?{query}"
        data = self._get(path)
        if not isinstance(data, dict):
            raise ActionError("hosted baseline-compatible response was not an object")
        return data

    def run_diff(self, api_diff_url: str) -> dict[str, Any]:
        data = self._get(api_diff_url)
        if not isinstance(data, dict):
            raise ActionError("hosted diff response was not an object")
        return data

    def policy_check(self, run_id: str) -> dict[str, Any]:
        """The governed gate's decision for ``run_id``, evaluated against the project's policy.

        The action never re-implements the policy; the server owns thresholds, budgets and
        statistics, and this is the one place that judgement is read from.
        """
        data = self._get(f"/runs/{quote(run_id)}/policy-check")
        if not isinstance(data, dict):
            raise ActionError("hosted policy-check response was not an object")
        return data

    def ci_preflight(self, project_slug: str, *, repo_private: bool) -> None:
        """Ask whether this job may run at all, before the suite spends anyone's money.

        One ``POST /runs/preflight``, addressed by the same ``org/project`` slug ``POST /runs``
        takes and authorized by the same ``run:create`` -- so the key that can push can always
        ask, and a key pinned to one project needs no org-wide listing first.

        Raises ``PreflightDenied`` on the server's 402. Every other ``HTTPError`` propagates
        unread, because what it means depends on inputs only ``run_preflight`` holds.
        """
        payload = json.dumps(
            {
                "project_slug": project_slug,
                "repo_private": repo_private,
                "parallelism": PREFLIGHT_PARALLELISM,
            }
        ).encode("utf-8")
        try:
            self._request("POST", self._url(PREFLIGHT_PATH), self._headers(), payload)
        except HTTPError as exc:
            if exc.code != 402:
                raise
            message, details = error_body(exc)
            raise PreflightDenied(
                message or "this run is not covered by the organization's EvalShift plan",
                details,
            ) from exc

    def _get(self, path_or_url: str) -> Any:
        """GET a hosted endpoint, turning a 403 into an error that says how to fix itself."""
        try:
            return self._request("GET", self._url(path_or_url), self._headers(), None)
        except HTTPError as exc:
            if exc.code != 403:
                raise
            detail = error_body_message(exc)
            hint = missing_permission_hint(detail) or KEY_ADVICE
            suffix = f": {detail}" if detail else ""
            raise ActionError(
                f"hosted EvalShift refused the request (HTTP 403){suffix}\n{hint}"
            ) from exc

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return f"{self.host}/{path_or_url.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        request: RequestFn | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._request = request or http_request

    def list_comments(self, repo: str, pull_number: int) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"{self.api_url}/repos/{repo}/issues/{pull_number}/comments",
            self._headers(),
            None,
        )
        if not isinstance(data, list):
            raise ActionError("GitHub comments response was not a list")
        return [item for item in data if isinstance(item, dict)]

    def create_comment(self, repo: str, pull_number: int, body: str) -> None:
        self._request(
            "POST",
            f"{self.api_url}/repos/{repo}/issues/{pull_number}/comments",
            self._headers(),
            json.dumps({"body": body}).encode("utf-8"),
        )

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._request(
            "PATCH",
            f"{self.api_url}/repos/{repo}/issues/comments/{comment_id}",
            self._headers(),
            json.dumps({"body": body}).encode("utf-8"),
        )

    def create_status(
        self,
        repo: str,
        sha: str,
        *,
        state: str,
        target_url: str,
        description: str,
        context: str,
    ) -> None:
        self._request(
            "POST",
            f"{self.api_url}/repos/{repo}/statuses/{sha}",
            self._headers(),
            json.dumps(
                {
                    "state": state,
                    "target_url": target_url,
                    "description": description[:140],
                    "context": context,
                }
            ).encode("utf-8"),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def _input(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(f"INPUT_{name}", default).strip()


def _bool_input(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _input(env, name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _release_tuple(version: str) -> tuple[int, ...]:
    """Leading numeric components of ``version``, for an ordering comparison.

    Pins are plain releases (``1.0.0``) in practice, but a pre-release or local
    segment must not crash the check, so everything from the first non-numeric
    component on is dropped. An unparseable pin yields ``()``, which compares
    below every real release -- callers treat that as "cannot vouch for it" and
    skip the guard rather than block on a string they do not understand.
    """
    parts: list[int] = []
    for chunk in version.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def _suite_selection(suite: str, suite_name: str, evalshift_version: str) -> tuple[str, str]:
    """Resolve the ``suite`` / ``suite-name`` inputs into exactly one selection.

    The two spellings do different things in the CLI, which is why only one may
    be set: ``--suite <path>`` names a file and nothing else, while
    ``--suite-name <key>`` also picks up that suite's own ``evaluators:`` block
    from ``evalshift.yaml``. Passing a path for a suite that wires its own
    evaluators scores it with the top-level set instead -- which is a green run
    with the wrong evaluators, or an empty ``scores.jsonl``, not an error. So
    setting both is refused rather than silently resolved by precedence.

    Args:
        suite: The ``suite`` input (a path), or ``""``.
        suite_name: The ``suite-name`` input (a ``suites:`` key), or ``""``.
        evalshift_version: The pinned CLI version, checked against
            :data:`MIN_SUITE_NAME_VERSION` before a name is accepted.

    Returns:
        ``(suite, suite_name)`` with exactly one non-empty.

    Raises:
        ActionError: If both inputs are set, or if ``suite-name`` is set on a
            pin too old to accept ``--suite-name``.
    """
    if suite and suite_name:
        raise ActionError(
            "inputs 'suite' and 'suite-name' are mutually exclusive -- set 'suite-name' "
            "(the `suites:` key from evalshift.yaml, which carries that suite's own "
            "evaluators) or 'suite' (a bare path), not both"
        )
    if not suite_name:
        return suite or DEFAULT_SUITE_PATH, ""
    pinned = _release_tuple(evalshift_version)
    if pinned and pinned < MIN_SUITE_NAME_VERSION:
        wanted = ".".join(str(part) for part in MIN_SUITE_NAME_VERSION)
        raise ActionError(
            f"input 'suite-name' needs an EvalShift CLI >= {wanted}, but "
            f"'evalshift-version' pins {evalshift_version}. Raise the pin, or select the "
            "suite with 'suite' (a path) instead."
        )
    return "", suite_name


def detect_context(env: Mapping[str, str], branch: str, base_branch: str) -> GitHubContext:
    event_name = env.get("GITHUB_EVENT_NAME", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    event = _read_event(env.get("GITHUB_EVENT_PATH"))
    pull_request = _dict_field(event, "pull_request")
    is_pr = event_name.startswith("pull_request")
    raw_number = event.get("number")
    pull_number = raw_number if isinstance(raw_number, int) else None
    head = _dict_field(pull_request, "head")
    base = _dict_field(pull_request, "base")
    sha = str(head.get("sha") or env.get("GITHUB_SHA") or "")
    resolved_branch = branch or str(head.get("ref") or env.get("GITHUB_HEAD_REF") or "")
    if not resolved_branch:
        resolved_branch = env.get("GITHUB_REF_NAME", "")
    resolved_base = base_branch or str(base.get("ref") or env.get("GITHUB_BASE_REF") or "")
    if not resolved_base:
        resolved_base = env.get("GITHUB_REF_NAME", "")
    return GitHubContext(
        event_name=event_name,
        repository=repository,
        sha=sha,
        branch=resolved_branch,
        base_branch=resolved_base,
        pull_number=pull_number,
        is_pull_request=is_pr,
    )


def _dict_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _list_field(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _read_event(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_run_id(runs_dir: Path) -> str:
    if not runs_dir.exists():
        raise ActionError(f"run directory {runs_dir} does not exist")
    candidates = [item for item in runs_dir.iterdir() if item.is_dir()]
    if not candidates:
        raise ActionError(f"no local EvalShift runs found in {runs_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime).name


def run_evalshift_commands(
    config: ActionConfig,
    *,
    cwd: Path,
    runner: RunnerFn | None = None,
    env: dict[str, str] | None = None,
) -> EvalShiftRunResult:
    run = runner or run_command
    # `is None`, not truthiness: an explicitly empty env means "start from nothing", and a
    # caller (a test, most of all) that asks for that must not silently get the real
    # environment back — that inheritance is what lets a test pass with the lines below removed.
    command_env = dict(os.environ if env is None else env)
    command_env["EVALSHIFT_HOST"] = config.host
    command_env["EVALSHIFT_TOKEN"] = config.token
    command_env["COLUMNS"] = CLI_CONSOLE_COLUMNS
    # One selection, built once and passed to both commands: the run and the push must load
    # the same suite the same way, and `push` re-resolves it to decide which evaluators the
    # bundle claims. See `_suite_selection` for why a name is not a path.
    selection = (
        ["--suite-name", config.suite_name] if config.suite_name else ["--suite", config.suite]
    )
    # `all`, not `compare`: the CLI renamed this command to `evalshift compare` and kept
    # `all` registered permanently as a hidden alias bound to the same function. The action
    # types the name that exists in EVERY installable CLI version -- including releases older
    # than the rename, which the default pin and any user pin may well be. Invoking the alias
    # costs one notice line on stderr and nothing else. Do not "modernise" this to `compare`
    # unless the minimum supported pin is raised past the release that introduced it.
    run(
        ["evalshift", "all", "--yes", "--config", config.config, *selection],
        cwd,
        command_env,
    )
    local_run_id = latest_run_id(cwd / ".evalshift" / "runs")
    push_cmd = [
        "evalshift",
        "push",
        # The local directory name, not the hosted id: this argument names the run to read
        # off this disk, and the server has not minted anything for it yet.
        local_run_id,
        "--config",
        config.config,
        *selection,
    ]
    if not config.create_project:
        push_cmd.append("--no-create-project")
    pushed = run(push_cmd, cwd, command_env)
    run_url = extract_url(pushed.stdout)
    if not run_url:
        raise ActionError("evalshift push did not print a hosted run URL")
    return EvalShiftRunResult(run_id=server_run_id_from_url(run_url), run_url=run_url)


def run_command(cmd: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(redact_text(completed.stdout, env), end="")
    if completed.stderr:
        print(redact_text(completed.stderr, env), end="", file=sys.stderr)
    if completed.returncode != 0:
        message = f"command failed ({completed.returncode}): {' '.join(cmd)}"
        # The CLI holds the token, so a `run:create` denial surfaces here rather than
        # on the action's own requests. Repeat it as guidance instead of leaving the
        # reader to spot one line of CLI output above a generic failure.
        hint = missing_permission_hint(f"{completed.stdout}\n{completed.stderr}")
        raise ActionError(f"{message}\n{hint}" if hint else message)
    return CommandResult(stdout=completed.stdout, returncode=completed.returncode)


def missing_permission_hint(text: str) -> str | None:
    """Guidance for a hosted permission denial found in ``text``, else ``None``."""
    match = PERMISSION_PATTERN.search(text)
    if match is None:
        return None
    return f"The EvalShift token is missing the '{match.group(1)}' permission. {KEY_ADVICE}"


def error_body(exc: HTTPError) -> tuple[str, dict[str, Any]]:
    """The hosted error's message and details, from a body that can only be read once."""
    try:
        raw = exc.read()
    except (AttributeError, OSError, ValueError):
        return "", {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw).strip(), {}
    if not isinstance(payload, dict):
        return "", {}
    error = _dict_field(payload, "error")
    details = _dict_field(error, "details")
    message = error.get("message")
    if isinstance(message, str):
        return message, details
    detail = payload.get("detail")
    return (detail if isinstance(detail, str) else ""), details


def error_body_message(exc: HTTPError) -> str:
    """The hosted error message carried by a failed response, or an empty string."""
    return error_body(exc)[0]


def project_ref_from_config(config_path: Path) -> tuple[str, str] | None:
    """The hosted ``(org, project)`` this config pushes to, or ``None`` when it says nothing.

    A config without a `project` key pushes to whatever `--project` or the bundle names, which
    the action cannot know before the CLI runs — so the preflight is skipped rather than
    guessed at.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = PROJECT_KEY_PATTERN.match(line)
        if match is not None:
            return match.group(1), match.group(2)
    return None


def run_preflight(
    hosted: Any,
    *,
    project_ref: tuple[str, str] | None,
    repo_private: bool,
    create_project: bool,
) -> PreflightDenied | None:
    """Ask hosted EvalShift whether this job may run, before the suite costs anything.

    The question is asked with the same slug and the same key the push will use, so each
    answer that the push would repeat -- after every model call in the suite has been paid
    for -- stops the job here instead:

    - 402: the plan does not cover this run. Returned as a denial for the caller to render.
    - 401: the token is not accepted at all. Raises ``ActionError``.
    - 403: the key lacks ``run:create``. Raises ``ActionError`` naming the permission.
    - 404 with ``create_project`` off: the push is forbidden to create the project, so it
      would 404 too. Raises ``ActionError``. With ``create_project`` on, the first push
      creates the project, so this is a ``::notice::`` and the run continues.

    Everything else fails open: a 405 from a server that predates the route, a 5xx, a 422, a
    timeout, a malformed body. A billing check that breaks every customer's CI when the
    billing service is down is worse than one that occasionally lets a run through, and the
    server enforces every limit again at upload.
    """
    if project_ref is None:
        return None
    project_slug = "/".join(project_ref)
    try:
        hosted.ci_preflight(project_slug, repo_private=repo_private)
    except PreflightDenied as denial:
        return denial
    except HTTPError as exc:
        _preflight_http_error(exc, project_slug=project_slug, create_project=create_project)
    # OSError covers URLError and a socket timeout; ValueError a malformed response body.
    except (OSError, ValueError) as exc:
        print(f"warning: plan preflight skipped: {exc}", file=sys.stderr)
    return None


def _preflight_http_error(exc: HTTPError, *, project_slug: str, create_project: bool) -> None:
    """Act on a non-402 preflight failure: raise for the ones the push would repeat, else warn."""
    host = _origin(exc.url)
    if exc.code == 401:
        detail = error_body_message(exc)
        suffix = f": {detail}" if detail else ""
        raise ActionError(
            f"hosted EvalShift at {host} rejected the token (HTTP 401){suffix}\n"
            "The 'token' input is not a live EvalShift key for this host: it is mistyped, "
            "revoked, past its rotation grace window, or was minted on a different 'host'. "
            f"{KEY_ADVICE}"
        ) from exc
    if exc.code == 403:
        detail = error_body_message(exc)
        suffix = f": {detail}" if detail else ""
        hint = missing_permission_hint(detail) or missing_permission_hint(
            "Permission denied: run:create"
        )
        raise ActionError(
            f"hosted EvalShift refused the plan preflight (HTTP 403){suffix}\n{hint}"
        ) from exc
    if exc.code == 404:
        if not create_project:
            raise ActionError(
                f"hosted EvalShift has no project '{project_slug}' this token can reach "
                "(HTTP 404), and create-project: false forbids the push to create it. Check "
                "the `project:` key in the EvalShift config, create the project in the web "
                "app, or use a key that is not pinned to a different project."
            ) from exc
        print(
            workflow_command(
                "notice",
                "EvalShift preflight",
                f"project '{project_slug}' is not on hosted EvalShift yet, or this token "
                "cannot see it (HTTP 404); the first push creates it, and plan limits are "
                "checked when the run is uploaded",
            )
        )
        return
    if exc.code == 405:
        print(
            f"warning: plan preflight skipped: hosted EvalShift at {host} predates "
            "POST /runs/preflight (HTTP 405); plan limits are still enforced when the run "
            "is uploaded",
            file=sys.stderr,
        )
        return
    print(f"warning: plan preflight skipped: {exc}", file=sys.stderr)


def _origin(url: str | None) -> str:
    """``scheme://host`` of ``url`` -- enough to say which server answered, nothing more."""
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else "the configured host"


def build_preflight_body(denial: PreflightDenied) -> str:
    """The denial as markdown, for the step summary and the PR comment alike."""
    details = denial.details
    lines = [
        COMMENT_MARKER,
        "## EvalShift did not run",
        "",
        denial.message,
        "",
        f"**Plan:** `{details.get('tier') or 'unknown'}`",
        f"**Blocked by:** `{details.get('feature') or 'plan limits'}`",
    ]
    limit = details.get("limit")
    if isinstance(limit, int):
        used = details.get("used")
        lines.append(f"**Limit:** {limit}" + (f" (used {used})" if isinstance(used, int) else ""))
    resets_at = details.get("resets_at")
    if isinstance(resets_at, str) and resets_at:
        lines.append(f"**Resets:** {resets_at}")
    upgrade_url = details.get("upgrade_url")
    if isinstance(upgrade_url, str) and upgrade_url:
        lines.append(f"**Upgrade:** [plans and billing]({upgrade_url})")
    return "\n".join(lines)


def workflow_command(kind: str, title: str, message: str) -> str:
    """A GitHub ``::<kind>::`` command. Workflow commands are one line, so newlines escape."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} title={title}::{escaped}"


def error_annotation(message: str) -> str:
    """A GitHub ``::error::`` command for the job's own failure."""
    return workflow_command("error", "EvalShift", message)


def write_step_summary(body: str, env: Mapping[str, str] | None = None) -> None:
    summary_path = (env if env is not None else os.environ).get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as fh:
        fh.write(f"{body}\n")


def report_preflight_denial(
    denial: PreflightDenied,
    config: ActionConfig,
    context: GitHubContext,
) -> None:
    """Say why the job stopped in all three places a developer might look."""
    body = build_preflight_body(denial)
    upgrade_url = denial.details.get("upgrade_url")
    annotation = denial.message
    if isinstance(upgrade_url, str) and upgrade_url:
        annotation = f"{annotation}\nUpgrade: {upgrade_url}"
    print(error_annotation(annotation))
    write_step_summary(body)
    if config.github_token and config.comment:
        upsert_pr_comment(GitHubClient(config.github_token), context, body)


def mask_secret(value: str) -> None:
    if value:
        print(f"::add-mask::{value}")


def redact_text(text: str, env: dict[str, str]) -> str:
    redacted = text
    for secret in _secret_values(env):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _secret_values(env: dict[str, str]) -> list[str]:
    values: list[str] = []
    for key, value in env.items():
        upper_key = key.upper()
        if not value or len(value) < 4:
            continue
        if "TOKEN" in upper_key or "SECRET" in upper_key or upper_key.endswith("API_KEY"):
            values.append(value)
    return values


def extract_url(output: str) -> str:
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return ""


def server_run_id_from_url(run_url: str) -> str:
    """The server-minted run id in a hosted run URL (``.../app/{org}/{project}/runs/<id>``).

    Raises rather than guesses. Every hosted call the action makes afterwards is keyed on this
    value, and a wrong one does not fail loudly: ``/runs/{id}/policy-check`` answers 404, which
    the gate reads as "this run has no stored policy decision" and degrades through. Better a
    named error here than a green check bought with the wrong id.

    Anchored on the server's full URL shape rather than on whatever segment ends the path or
    the first ``/runs/`` in it. A ``.../projects/<uuid>`` URL is the right shape and the wrong
    thing; ``.../runs/<a>/diff/<b>`` ends on the id of the *other* side of the comparison; and
    an org slugged ``runs`` in front of a UUID-shaped project slug puts a decoy ``/runs/<36
    chars>`` to the left of the real one.

    Matched against the RAW path, deliberately un-unquoted. Do not "restore" a ``unquote()``
    here: decoding first would let a ``%2Fruns%2F<uuid>`` sequence inside some other segment
    forge the very ``/runs/`` boundary this function exists to enforce. A genuinely
    percent-encoded id is not something the CLI prints, and refusing one loudly is the safe
    direction.
    """
    match = RUN_URL_ID_PATTERN.search(urlsplit(run_url).path)
    run_id = match.group(1) if match else ""
    if not SERVER_RUN_ID_PATTERN.match(run_id):
        raise ActionError(
            f"could not read a server run id out of the hosted run URL: {run_url!r} "
            "(expected an /app/<org>/<project>/runs/<uuid> path)"
        )
    return run_id


def fetch_policy_check(hosted: Any, run_id: str) -> tuple[dict[str, Any] | None, str]:
    """The governed decision for ``run_id``, or ``(None, reason)`` when there is not one.

    Deliberately never raises. A policy endpoint that 404s, times out, or answers without a
    decision is a real condition the caller has to degrade through — but degrading into a
    silent green check is how a regression merges. The reason travels back so the fallback can
    be named in the log, the commit status and the PR comment.
    """
    try:
        payload = hosted.policy_check(run_id)
    except HTTPError as exc:
        reason = (
            "this run has no stored policy decision (HTTP 404)"
            if exc.code == 404
            else f"hosted policy check returned HTTP {exc.code}"
        )
    # OSError covers URLError; ActionError covers a wrapped 403; ValueError a malformed body.
    except (OSError, ValueError, ActionError) as exc:
        reason = f"hosted policy check failed: {exc}"
    else:
        if str(payload.get("status") or "").strip():
            return payload, ""
        reason = "hosted policy check returned no decision for this run"
    print(
        f"warning: {reason}; falling back to fail-on: {POLICY_FALLBACK_MODE}",
        file=sys.stderr,
    )
    return None, reason


def evaluate_gating(
    diff: dict[str, Any] | None,
    fail_on: str,
    *,
    policy: dict[str, Any] | None = None,
    policy_unavailable: str = "",
    require_policy: bool = False,
) -> GatingResult:
    """Decide whether this job fails, and record why.

    In every mode but ``policy`` the diff decides. In ``policy`` the diff is still reported —
    the comment renders it — but the governed server gate is what the exit code follows.
    """
    aggregate = _dict_field(diff or {}, "aggregate_delta")
    regression_count = int(aggregate.get("regressions") or 0)
    slices = _list_field(diff or {}, "per_slice_deltas")
    slice_regressions = sorted(
        (
            item
            for item in slices
            if isinstance(item, dict) and _float(item.get("pass_rate_delta")) < 0
        ),
        key=lambda item: _float(item.get("pass_rate_delta")),
    )
    top_slice_regressions = slice_regressions[:MAX_SLICE_ROWS]
    if fail_on == "policy":
        return _policy_gating(
            policy,
            policy_unavailable,
            regression_count=regression_count,
            top_slice_regressions=top_slice_regressions,
            require_policy=require_policy,
        )
    should_fail = False
    if fail_on == "regression":
        should_fail = regression_count > 0
    elif fail_on == "any-slice-regression":
        should_fail = bool(slice_regressions)
    return GatingResult(
        conclusion="failure" if should_fail else "success",
        should_fail=should_fail,
        regression_count=regression_count,
        top_slice_regressions=top_slice_regressions,
        mode=fail_on,
    )


def _policy_gating(
    policy: dict[str, Any] | None,
    policy_unavailable: str,
    *,
    regression_count: int,
    top_slice_regressions: list[dict[str, Any]],
    require_policy: bool = False,
) -> GatingResult:
    """Turn a ``policy-check`` response into a gate decision, or degrade loudly without one.

    ``require_policy`` is the ``require-policy`` input, and it governs exactly one case: a run
    pushed with no policy at all. It is never a second opinion on a verdict the policy did
    reach, and it says nothing about a policy check that could not be read -- that is the
    fallback path above, which already refuses to go quietly green.
    """
    if policy is None:
        reason = policy_unavailable or "hosted EvalShift returned no policy decision"
        should_fail = regression_count > 0
        return GatingResult(
            conclusion="failure" if should_fail else "success",
            should_fail=should_fail,
            regression_count=regression_count,
            top_slice_regressions=top_slice_regressions,
            mode="policy",
            summary=(
                f"policy check unavailable — {reason}; fell back to fail-on: {POLICY_FALLBACK_MODE}"
            ),
            policy_unavailable_reason=reason,
        )
    status = str(policy.get("status") or "").strip().lower()
    policy_reason = _text(policy.get("reason"))
    policy_source = _text(policy.get("policy_source"))
    source = policy_source or "the project's policy"
    # Not a verdict: the run carried no policy, so nothing was gated. Said in its own words
    # because the shared `inconclusive` wording ("could not decide") describes an answer, and
    # this is the absence of one -- with a fix the reader can apply in a single yaml key.
    ungated = status == "inconclusive" and policy_source == POLICY_SOURCE_NONE
    if ungated:
        print(NO_POLICY_ANNOTATION)
        should_fail = require_policy
        summary = NO_POLICY_REQUIRED_SUMMARY if require_policy else NO_POLICY_SUMMARY
    elif status == "fail":
        should_fail = True
        summary = f"the {source} gate failed"
    elif status == "pass":
        should_fail = False
        summary = f"the {source} gate passed"
    elif status == "conditional_pass":
        # A pass, not a non-answer: budgets held and nothing critical or high regressed. The
        # caveat is real enough to say out loud and not real enough to fail a merge on.
        should_fail = False
        summary = f"the {source} gate passed, with caveats"
    elif status == "inconclusive":
        should_fail = False
        summary = f"the {source} gate could not decide (inconclusive); not failing the job"
    else:
        # An open status set: the server grows new verdicts, and an older action pinned in a
        # workflow still has to behave. Unknown is undecided, never a pass.
        should_fail = False
        summary = (
            f"the {source} gate returned an unrecognized status '{status or 'missing'}'; "
            "treated as undecided, not as a pass"
        )
    if policy_reason and not ungated:
        # The ungated sentence is deliberately left alone. The server's reason for this case is
        # the same instruction in other words, and the commit-status description it feeds is cut
        # at 140 characters -- appending would push the fix off the end. The reason still travels
        # verbatim into the PR comment through `policy_reason`.
        summary = f"{summary}: {policy_reason}"
    return GatingResult(
        conclusion="failure" if should_fail else "success",
        should_fail=should_fail,
        regression_count=regression_count,
        top_slice_regressions=top_slice_regressions,
        mode="policy",
        summary=summary,
        policy_status=status,
        policy_verdict=_text(policy.get("verdict")),
        policy_reason=policy_reason,
        policy_source=policy_source,
        budgets=[item for item in _list_field(policy, "budgets") if isinstance(item, dict)],
        blocking_regressions=[
            item for item in _list_field(policy, "blocking_regressions") if isinstance(item, dict)
        ],
    )


def _float(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _text(value: Any) -> str:
    """A server-supplied string, flattened to one line so it cannot break a status or a table."""
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _cell(value: Any, default: str = "—") -> str:
    """``value`` as a markdown table cell: one line, with the column separator neutralised."""
    text = _text(value)
    return text.replace("|", "\\|") if text else default


def _metric(value: Any) -> str:
    """A budget number for display. Non-numeric — including a missing bound — reads ``n/a``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "n/a"
    return f"{value:.4g}"


def build_comment_body(
    *,
    run_url: str,
    diff_url: str | None,
    baseline: dict[str, Any] | None,
    diff: dict[str, Any] | None,
    gating: GatingResult,
) -> str:
    lines = [
        COMMENT_MARKER,
        "## EvalShift regression check",
        "",
        f"**Conclusion:** `{gating.conclusion}`",
        f"**Hosted run:** [open run]({run_url})",
        f"**Regressions:** {gating.regression_count}",
    ]
    if diff_url:
        lines.append(f"**Diff:** [compare to baseline]({diff_url})")
    lines.extend(_policy_sections(gating))
    if baseline is None or diff is None:
        lines.extend(["", "No compatible baseline run was found on the base branch."])
        return "\n".join(lines)

    aggregate = _dict_field(diff, "aggregate_delta")
    pass_rate_delta = _format_percent_delta(_float(aggregate.get("pass_rate_delta")))
    lines.extend(
        [
            f"**Pass-rate movement:** {pass_rate_delta}",
            "",
            "| Slice | Pass-rate delta |",
            "| --- | ---: |",
        ]
    )
    if not gating.top_slice_regressions:
        lines.append("| No regressed slices | 0 pts |")
    else:
        for item in gating.top_slice_regressions:
            lines.append(
                f"| {_cell(item.get('slice'), 'uncategorized')} | "
                f"{_format_percent_delta(_float(item.get('pass_rate_delta')))} |"
            )
    return "\n".join(lines)


def _policy_sections(gating: GatingResult) -> list[str]:
    """The governed decision, its budgets and its blocking regressions, as markdown.

    Empty in the diff-only modes: nothing asked the server for a policy, so there is nothing
    honest to render.
    """
    if gating.mode != "policy":
        return []
    if gating.policy_unavailable_reason:
        return [
            "",
            f"> **The policy gate did not run** — {gating.policy_unavailable_reason}.",
            f"> This check fell back to `fail-on: {POLICY_FALLBACK_MODE}`, so it reflects the "
            "diff alone — not your migration policy.",
            "",
        ]
    lines = ["", f"**Policy decision:** `{gating.policy_status or 'missing'}`"]
    if gating.policy_source:
        lines[-1] += f" (from `{gating.policy_source}`)"
    if gating.policy_reason:
        lines.append(f"**Why:** {gating.policy_reason}")
    if gating.policy_ungated:
        lines.extend(
            [
                "",
                "> **Nothing gates this PR.** No migration policy was pushed with this run, so "
                "this check reports the diff and stops there. Add `migration_policy` to "
                "`evalshift.yaml` and push again — the policy travels with the run.",
            ]
        )
    elif gating.policy_caveated:
        lines.extend(
            [
                "",
                "> **The policy gate passed, with caveats.** Every budget held and nothing "
                "critical or high regressed, so this is not a gate failure — but the run is "
                "not clean either. Read the reason above before merging.",
            ]
        )
    elif not gating.policy_decided:
        lines.extend(
            [
                "",
                "> **The policy gate could not decide.** This check is not a pass — the "
                "migration policy reached no verdict, and the job was not failed on a "
                "non-answer.",
            ]
        )
    lines.extend(_budget_table(gating.budgets))
    lines.extend(_blocking_regression_table(gating.blocking_regressions))
    # A trailing blank keeps whatever the caller appends next off the last table row.
    lines.append("")
    return lines


def _budget_table(budgets: list[dict[str, Any]]) -> list[str]:
    if not budgets:
        return []
    # Failing budgets first: the cap must never be what hides the reason the job went red.
    ordered = [item for item in budgets if item.get("passed") is False]
    ordered += [item for item in budgets if item.get("passed") is not False]
    shown = ordered[:MAX_BUDGET_ROWS]
    lines = [
        "",
        "### Policy budgets",
        "",
        "| Budget | Scope | Observed | Allowed | Result |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for budget in shown:
        passed = budget.get("passed")
        result = "pass" if passed else ("fail" if passed is False else "unknown")
        # `conclusive: false` means the interval is too wide to tell — say so rather than let
        # a coin-flip render as a clean pass.
        if budget.get("conclusive") is False:
            result += " (not confident)"
        lines.append(
            f"| {_cell(budget.get('name'))} | {_cell(budget.get('scope'))} | "
            f"{_metric(budget.get('observed'))} | {_metric(budget.get('allowed'))} | {result} |"
        )
    hidden = ordered[len(shown) :]
    if hidden:
        # Per-slice budgets (a policy with slice overrides emits one row per slice) can push
        # more failures past the cap than fit above it. A bare count would read as "the rest
        # were fine", which is the exact misreading this table exists to prevent.
        hidden_failures = sum(1 for item in hidden if item.get("passed") is False)
        note = f"{len(hidden)} more budgets not shown"
        if hidden_failures:
            note += f", {hidden_failures} of them failing"
        lines.extend(["", f"{note} — see the hosted run."])
    return lines


def _blocking_regression_table(regressions: list[dict[str, Any]]) -> list[str]:
    if not regressions:
        return []
    shown = regressions[:MAX_BLOCKING_ROWS]
    lines = [
        "",
        "### Blocking regressions",
        "",
        "| Prompt | Evaluator | Slice | Severity | Score delta |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for item in shown:
        lines.append(
            f"| {_cell(item.get('prompt_id'))} | {_cell(item.get('evaluator_name'))} | "
            f"{_cell(item.get('slice_name'), 'uncategorized')} | {_cell(item.get('severity'))} | "
            f"{_metric(item.get('delta_avg_score'))} |"
        )
    omitted = len(regressions) - len(shown)
    if omitted:
        lines.extend(["", f"{omitted} more blocking regressions not shown — see the hosted run."])
    return lines


def _format_percent_delta(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{round(value * 100)} pts"


def upsert_pr_comment(github: Any, context: GitHubContext, body: str) -> None:
    if not context.is_pull_request or context.pull_number is None:
        return
    try:
        comments = github.list_comments(context.repository, context.pull_number)
        for comment in comments:
            user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
            is_bot_comment = user.get("type") == "Bot"
            if is_bot_comment and COMMENT_MARKER in str(comment.get("body") or ""):
                github.update_comment(context.repository, int(comment["id"]), body)
                return
        github.create_comment(context.repository, context.pull_number, body)
    except HTTPError as exc:
        if exc.code in {403, 404}:
            print(f"warning: could not upsert PR comment: HTTP {exc.code}", file=sys.stderr)
            return
        raise


def set_commit_status(
    github: Any,
    context: GitHubContext,
    gating: GatingResult,
    *,
    target_url: str,
) -> None:
    detail = gating.summary or f"{gating.regression_count} regression(s)"
    try:
        github.create_status(
            context.repository,
            context.sha,
            state=gating.conclusion,
            target_url=target_url,
            description=f"EvalShift {gating.conclusion}: {detail}",
            context=STATUS_CONTEXT,
        )
    except HTTPError as exc:
        if exc.code in {403, 404}:
            print(f"warning: could not set commit status: HTTP {exc.code}", file=sys.stderr)
            return
        raise


def http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    data: bytes | None = None,
) -> Any:
    request_headers = dict(headers)
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=data, headers=request_headers, method=method)
    with urlopen(request, timeout=30) as response:
        body = response.read()
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_outputs(outputs: dict[str, Any], env: dict[str, str] | None = None) -> None:
    # `is None`, not truthiness — same rule as `run_evalshift_commands`. An explicitly empty
    # env means "no GITHUB_OUTPUT"; falling through to `os.environ` would hand a caller that
    # asked for nothing the live runner's output file, which always has it set.
    output_path = (os.environ if env is None else env).get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={value}\n")


# What a job that never ran the suite reports: nothing was pushed, and it failed.
STOPPED_OUTPUTS: dict[str, Any] = {
    "run_url": "",
    "diff_url": "",
    "run_id": "",
    "regression_count": 0,
    "conclusion": "failure",
}


def main() -> int:
    try:
        config = ActionConfig.from_env()
        mask_secret(config.token)
        mask_secret(config.github_token)
        context = detect_context(os.environ, config.branch, config.base_branch)
        hosted = HostedClient(config.host, config.token)
        try:
            denial = run_preflight(
                hosted,
                project_ref=project_ref_from_config(Path(config.config)),
                repo_private=config.repo_private,
                create_project=config.create_project,
            )
        except ActionError:
            # Stopped before the suite: the same outputs as a denial, so a workflow reading
            # `conclusion` sees a failure rather than an empty string.
            write_outputs(STOPPED_OUTPUTS)
            raise
        if denial is not None:
            report_preflight_denial(denial, config, context)
            write_outputs(STOPPED_OUTPUTS)
            return 1
        run = run_evalshift_commands(config, cwd=Path.cwd())
        baseline_payload = (
            hosted.baseline_compatible(run.run_id, context.base_branch)
            if context.base_branch
            else {}
        )
        baseline = baseline_payload.get("baseline_run") if baseline_payload else None
        api_diff_url = baseline_payload.get("api_diff_url") if baseline_payload else None
        web_diff_url = baseline_payload.get("web_diff_url") if baseline_payload else None
        diff = hosted.run_diff(str(api_diff_url)) if api_diff_url else None
        policy: dict[str, Any] | None = None
        policy_unavailable = ""
        if config.fail_on == "policy":
            policy, policy_unavailable = fetch_policy_check(hosted, run.run_id)
        gating = evaluate_gating(
            diff,
            config.fail_on,
            policy=policy,
            policy_unavailable=policy_unavailable,
            require_policy=config.require_policy,
        )
        if gating.summary:
            print(f"EvalShift gate: {gating.summary}")
        target_url = str(web_diff_url or run.run_url)
        write_outputs(
            {
                "run_url": run.run_url,
                "diff_url": web_diff_url or "",
                "run_id": run.run_id,
                "regression_count": gating.regression_count,
                "conclusion": gating.conclusion,
            }
        )
        if config.github_token:
            github = GitHubClient(config.github_token)
            if config.comment:
                upsert_pr_comment(
                    github,
                    context,
                    build_comment_body(
                        run_url=run.run_url,
                        diff_url=str(web_diff_url) if web_diff_url else None,
                        baseline=baseline if isinstance(baseline, dict) else None,
                        diff=diff,
                        gating=gating,
                    ),
                )
            set_commit_status(github, context, gating, target_url=target_url)
        return 1 if gating.should_fail else 0
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
