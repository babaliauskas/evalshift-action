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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

COMMENT_MARKER = "<!-- evalshift:comment -->"
STATUS_CONTEXT = "evalshift/regression"
DEFAULT_HOST = "https://api.evalshift.dev"
DEFAULT_EVALSHIFT_VERSION = "0.11.0"

# One EvalShift run per job. A matrix asks once per job, and the server counts the
# in-flight runs itself, so declaring anything larger here would be a guess.
PREFLIGHT_PARALLELISM = 1

# `project: org/project` at the top level of evalshift.yaml. Read with a regex rather than a
# YAML parser because the action ships with no dependencies, and the CLI constrains the key to
# exactly this shape. Anything this misses costs only the preflight, which is fail-open.
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
    fail_on: str
    branch: str
    base_branch: str
    create_project: bool
    comment: bool
    github_token: str
    # Asserted by the workflow, not verified by EvalShift. Defaults to public: the server
    # records the first `true` permanently, so guessing `true` would be the costly guess.
    repo_private: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ActionConfig:
        source: Mapping[str, str] = env if env is not None else os.environ
        token = _input(source, "TOKEN")
        if not token:
            raise ActionError("input 'token' is required")
        fail_on = _input(source, "FAIL_ON", "regression")
        if fail_on not in {"never", "regression", "any-slice-regression"}:
            raise ActionError(
                "input 'fail-on' must be one of: never, regression, any-slice-regression"
            )
        return cls(
            token=token,
            host=_input(source, "HOST", DEFAULT_HOST).rstrip("/"),
            config=_input(source, "CONFIG", "evalshift.yaml"),
            suite=_input(source, "SUITE", "golden.jsonl"),
            evalshift_version=_input(source, "EVALSHIFT_VERSION", DEFAULT_EVALSHIFT_VERSION),
            fail_on=fail_on,
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
    run_id: str
    run_url: str


@dataclass(frozen=True)
class GatingResult:
    conclusion: str
    should_fail: bool
    regression_count: int
    top_slice_regressions: list[dict[str, Any]]


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

    def find_project_id(self, org_slug: str, project_slug: str) -> str | None:
        """The hosted id of ``org_slug/project_slug``, or ``None`` when it does not exist yet."""
        data = self._request(
            "GET",
            self._url(f"/orgs/{quote(org_slug)}/projects"),
            self._headers(),
            None,
        )
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict) or item.get("slug") != project_slug:
                continue
            project_id = item.get("id")
            return str(project_id) if project_id else None
        return None

    def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
        """Ask whether this job may run at all, before the suite spends anyone's money.

        Raises ``PreflightDenied`` on the server's 402. Every other failure is left to the
        caller, which treats it as an infrastructure problem and lets the run continue.
        """
        payload = json.dumps(
            {"repo_private": repo_private, "parallelism": PREFLIGHT_PARALLELISM}
        ).encode("utf-8")
        try:
            self._request(
                "POST",
                self._url(f"/projects/{quote(project_id)}/ci-preflight"),
                self._headers(),
                payload,
            )
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
    command_env = dict(env or os.environ)
    command_env["EVALSHIFT_HOST"] = config.host
    command_env["EVALSHIFT_TOKEN"] = config.token
    run(
        ["evalshift", "all", "--yes", "--config", config.config, "--suite", config.suite],
        cwd,
        command_env,
    )
    run_id = latest_run_id(cwd / ".evalshift" / "runs")
    push_cmd = [
        "evalshift",
        "push",
        run_id,
        "--config",
        config.config,
        "--suite",
        config.suite,
    ]
    if not config.create_project:
        push_cmd.append("--no-create-project")
    pushed = run(push_cmd, cwd, command_env)
    run_url = extract_url(pushed.stdout)
    if not run_url:
        raise ActionError("evalshift push did not print a hosted run URL")
    return EvalShiftRunResult(run_id=run_id, run_url=run_url)


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
) -> PreflightDenied | None:
    """Ask hosted EvalShift whether this job may run, before the suite costs anything.

    Fail-closed on billing, fail-open on infrastructure: a 402 comes back as a denial the
    caller must act on, while an outage, an unknown project, or a token that cannot list
    projects only prints a warning. A billing check that breaks every customer's CI when the
    billing service is down is worse than one that occasionally lets a run through.
    """
    if project_ref is None:
        return None
    org_slug, project_slug = project_ref
    try:
        project_id = hosted.find_project_id(org_slug, project_slug)
        if project_id is None:
            # Nothing to check yet: the project is created by the first `evalshift push`,
            # and the server gates that upload on its own.
            return None
        hosted.ci_preflight(project_id, repo_private=repo_private)
    except PreflightDenied as denial:
        return denial
    # OSError covers HTTPError and URLError; ActionError covers a wrapped 403.
    except (OSError, ValueError, ActionError) as exc:
        print(f"warning: plan preflight skipped: {exc}", file=sys.stderr)
    return None


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


def error_annotation(message: str) -> str:
    """A GitHub ``::error::`` command. Workflow commands are one line, so newlines escape."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title=EvalShift::{escaped}"


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


def evaluate_gating(diff: dict[str, Any] | None, fail_on: str) -> GatingResult:
    if diff is None:
        return GatingResult("success", False, 0, [])
    aggregate = _dict_field(diff, "aggregate_delta")
    regression_count = int(aggregate.get("regressions") or 0)
    slices = _list_field(diff, "per_slice_deltas")
    slice_regressions = [
        item
        for item in slices
        if isinstance(item, dict) and _float(item.get("pass_rate_delta")) < 0
    ]
    slice_regressions.sort(key=lambda item: _float(item.get("pass_rate_delta")))
    top_slice_regressions = slice_regressions[:5]
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
    )


def _float(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


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
                f"| {item.get('slice', 'uncategorized')} | "
                f"{_format_percent_delta(_float(item.get('pass_rate_delta')))} |"
            )
    return "\n".join(lines)


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
    try:
        github.create_status(
            context.repository,
            context.sha,
            state=gating.conclusion,
            target_url=target_url,
            description=(
                f"EvalShift {gating.conclusion}: "
                f"{gating.regression_count} regression(s)"
            ),
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
    output_path = (env or os.environ).get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={value}\n")


def main() -> int:
    try:
        config = ActionConfig.from_env()
        mask_secret(config.token)
        mask_secret(config.github_token)
        context = detect_context(os.environ, config.branch, config.base_branch)
        hosted = HostedClient(config.host, config.token)
        denial = run_preflight(
            hosted,
            project_ref=project_ref_from_config(Path(config.config)),
            repo_private=config.repo_private,
        )
        if denial is not None:
            report_preflight_denial(denial, config, context)
            write_outputs(
                {
                    "run_url": "",
                    "diff_url": "",
                    "run_id": "",
                    "regression_count": 0,
                    "conclusion": "failure",
                }
            )
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
        gating = evaluate_gating(diff, config.fail_on)
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
