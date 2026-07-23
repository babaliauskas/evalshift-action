from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evalshift_action as action


def test_action_manifest_quotes_descriptions_with_colons() -> None:
    manifest = Path(__file__).resolve().parents[1] / "action.yml"
    offenders: list[str] = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("description:"):
            continue
        value = stripped.removeprefix("description:").strip()
        if ": " in value and not value.startswith(('"', "'")):
            offenders.append(f"{line_number}: {stripped}")

    assert offenders == []


def _manifest_input_default(name: str) -> str:
    """Read an input default out of action.yml without a YAML dependency."""
    manifest = Path(__file__).resolve().parents[1] / "action.yml"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{name}:":
            continue
        for follower in lines[index + 1 :]:
            stripped = follower.strip()
            if stripped.startswith("default:"):
                return stripped.removeprefix("default:").strip().strip("\"'")
            if follower and not follower.startswith("    "):
                break
    raise AssertionError(f"no default found for input '{name}' in action.yml")


def test_script_default_version_matches_action_manifest() -> None:
    """The manifest always supplies the version, but a drifting fallback misleads readers."""
    assert _manifest_input_default("evalshift-version") == action.DEFAULT_EVALSHIFT_VERSION


def test_action_config_defaults_to_current_evalshift_release() -> None:
    config = action.ActionConfig.from_env({"INPUT_TOKEN": "es_secret"})

    assert config.evalshift_version == "0.8.0"


def test_detect_context_uses_pull_request_event_payload(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "number": 42,
                "pull_request": {
                    "head": {"sha": "b" * 40, "ref": "feature/model-swap"},
                    "base": {"ref": "main"},
                },
            }
        ),
        encoding="utf-8",
    )
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": "acme/repo",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_HEAD_REF": "fallback-head",
        "GITHUB_BASE_REF": "fallback-base",
    }

    context = action.detect_context(env, branch="", base_branch="")

    assert context.is_pull_request is True
    assert context.pull_number == 42
    assert context.sha == "b" * 40
    assert context.branch == "feature/model-swap"
    assert context.base_branch == "main"
    assert context.repository == "acme/repo"


def test_detect_context_uses_push_ref_and_explicit_base_branch() -> None:
    env = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "acme/repo",
        "GITHUB_SHA": "c" * 40,
        "GITHUB_REF_NAME": "main",
    }

    context = action.detect_context(env, branch="", base_branch="stable")

    assert context.is_pull_request is False
    assert context.pull_number is None
    assert context.branch == "main"
    assert context.base_branch == "stable"


def test_latest_run_id_uses_most_recent_run_directory(tmp_path: Path) -> None:
    runs = tmp_path / ".evalshift" / "runs"
    older = runs / "run-old"
    newer = runs / "run-new"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))

    assert action.latest_run_id(runs) == "run-new"


def test_run_evalshift_commands_runs_all_then_push(tmp_path: Path) -> None:
    runs = tmp_path / ".evalshift" / "runs"
    (runs / "run-1").mkdir(parents=True)
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> action.CommandResult:
        calls.append(cmd)
        envs.append(env)
        stdout = (
            "https://app.evalshift.dev/app/acme/project/runs/run-1\n"
            if cmd[1] == "push"
            else ""
        )
        return action.CommandResult(stdout=stdout, returncode=0)

    config = action.ActionConfig(
        token="es_secret",
        host="https://api.evalshift.dev",
        config="evalshift.yaml",
        suite="golden.jsonl",
        evalshift_version="0.4.0",
        fail_on="regression",
        branch="",
        base_branch="main",
        create_project=False,
        comment=True,
        github_token="ghs_secret",
    )

    result = action.run_evalshift_commands(config, cwd=tmp_path, runner=fake_runner, env={})

    assert result.run_id == "run-1"
    assert result.run_url == "https://app.evalshift.dev/app/acme/project/runs/run-1"
    assert calls == [
        ["evalshift", "all", "--yes", "--config", "evalshift.yaml", "--suite", "golden.jsonl"],
        [
            "evalshift",
            "push",
            "run-1",
            "--config",
            "evalshift.yaml",
            "--suite",
            "golden.jsonl",
            "--no-create-project",
        ],
    ]
    # Token + host travel only via env, never in argv.
    for cmd, env in zip(calls, envs, strict=True):
        assert "es_secret" not in cmd
        assert env["EVALSHIFT_TOKEN"] == "es_secret"
        assert env["EVALSHIFT_HOST"] == "https://api.evalshift.dev"


def test_mask_secret_emits_github_mask_command(capsys: pytest.CaptureFixture[str]) -> None:
    action.mask_secret("es_secret")

    assert "::add-mask::es_secret" in capsys.readouterr().out


def test_run_command_redacts_secret_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Completed:
        stdout = "hosted token es_secret\n"
        stderr = "github token ghs_secret\n"
        returncode = 0

    def fake_run(*args: Any, **kwargs: Any) -> Completed:
        return Completed()

    monkeypatch.setattr(action.subprocess, "run", fake_run)

    result = action.run_command(
        ["evalshift", "push", "run-1"],
        tmp_path,
        {"EVALSHIFT_TOKEN": "es_secret", "INPUT_GITHUB_TOKEN": "ghs_secret"},
    )

    captured = capsys.readouterr()
    assert result.stdout == "hosted token es_secret\n"
    assert "es_secret" not in captured.out
    assert "ghs_secret" not in captured.err
    assert "<redacted>" in captured.out
    assert "<redacted>" in captured.err


def test_hosted_client_calls_baseline_and_diff_endpoints() -> None:
    requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> dict[str, Any]:
        requests.append((method, url, headers, data))
        if "baseline-compatible" in url:
            return {
                "baseline_run": {"id": "base"},
                "compatibility": "direct",
                "api_diff_url": "/runs/base/diff/candidate",
                "web_diff_url": "https://app.test/diff",
            }
        return {
            "run_a_id": "base",
            "run_b_id": "candidate",
            "compatibility": "direct",
            "aggregate_delta": {"regressions": 2, "pass_rate_delta": -0.2},
            "per_slice_deltas": [{"slice": "security", "pass_rate_delta": -0.5}],
        }

    client = action.HostedClient("https://api.evalshift.dev/", "es_secret", request=fake_request)

    baseline = client.baseline_compatible("candidate", "main")
    diff = client.run_diff("/runs/base/diff/candidate")

    assert baseline["baseline_run"]["id"] == "base"
    assert diff["aggregate_delta"]["regressions"] == 2
    assert requests[0][0] == "GET"
    assert requests[0][1] == (
        "https://api.evalshift.dev/runs/candidate/baseline-compatible?branch=main"
    )
    assert requests[0][2]["Authorization"] == "Bearer es_secret"
    assert requests[1][1] == "https://api.evalshift.dev/runs/base/diff/candidate"


@pytest.mark.parametrize(
    ("fail_on", "expected_fail"),
    [
        ("never", False),
        ("regression", True),
        ("any-slice-regression", True),
    ],
)
def test_evaluate_gating_modes(fail_on: str, expected_fail: bool) -> None:
    diff = {
        "aggregate_delta": {"regressions": 1, "pass_rate_delta": -0.1},
        "per_slice_deltas": [
            {"slice": "security", "pass_rate_delta": -0.2},
            {"slice": "routine", "pass_rate_delta": 0.1},
        ],
    }

    result = action.evaluate_gating(diff, fail_on)

    assert result.regression_count == 1
    assert result.should_fail is expected_fail
    assert result.conclusion == ("failure" if expected_fail else "success")
    assert result.top_slice_regressions[0]["slice"] == "security"


def test_evaluate_gating_passes_without_baseline() -> None:
    result = action.evaluate_gating(None, "regression")

    assert result.regression_count == 0
    assert result.should_fail is False
    assert result.conclusion == "success"
    assert result.top_slice_regressions == []


@dataclass
class FakeGitHub:
    comments: list[dict[str, Any]]
    created_body: str | None = None
    updated: tuple[int, str] | None = None
    statuses: list[dict[str, Any]] | None = None

    def list_comments(self, repo: str, pull_number: int) -> list[dict[str, Any]]:
        assert repo == "acme/repo"
        assert pull_number == 42
        return self.comments

    def create_comment(self, repo: str, pull_number: int, body: str) -> None:
        self.created_body = body

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self.updated = (comment_id, body)

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
        if self.statuses is None:
            self.statuses = []
        self.statuses.append(
            {
                "repo": repo,
                "sha": sha,
                "state": state,
                "target_url": target_url,
                "description": description,
                "context": context,
            }
        )


def test_upsert_comment_updates_existing_marker_comment() -> None:
    github = FakeGitHub(
        comments=[
            {"id": 7, "body": "old\n<!-- evalshift:comment -->", "user": {"type": "Bot"}},
        ]
    )
    context = action.GitHubContext(
        event_name="pull_request",
        repository="acme/repo",
        sha="a" * 40,
        branch="feature",
        base_branch="main",
        pull_number=42,
        is_pull_request=True,
    )

    action.upsert_pr_comment(github, context, "new body")

    assert github.updated == (7, "new body")
    assert github.created_body is None


def test_upsert_comment_creates_when_marker_missing() -> None:
    github = FakeGitHub(comments=[])
    context = action.GitHubContext(
        event_name="pull_request",
        repository="acme/repo",
        sha="a" * 40,
        branch="feature",
        base_branch="main",
        pull_number=42,
        is_pull_request=True,
    )

    action.upsert_pr_comment(github, context, "new body")

    assert github.created_body == "new body"
    assert github.updated is None


def test_upsert_comment_does_not_update_human_marker_comment() -> None:
    github = FakeGitHub(
        comments=[
            {"id": 7, "body": "<!-- evalshift:comment -->", "user": {"type": "User"}},
        ]
    )
    context = action.GitHubContext(
        event_name="pull_request",
        repository="acme/repo",
        sha="a" * 40,
        branch="feature",
        base_branch="main",
        pull_number=42,
        is_pull_request=True,
    )

    action.upsert_pr_comment(github, context, "new body")

    assert github.updated is None
    assert github.created_body == "new body"


def test_set_status_warns_on_permission_error(capsys: pytest.CaptureFixture[str]) -> None:
    class ForbiddenGitHub(FakeGitHub):
        def create_status(self, *args: Any, **kwargs: Any) -> None:
            raise HTTPError("https://api.github.test", 403, "forbidden", {}, None)

    github = ForbiddenGitHub(comments=[])
    context = action.GitHubContext(
        event_name="pull_request",
        repository="acme/repo",
        sha="a" * 40,
        branch="feature",
        base_branch="main",
        pull_number=42,
        is_pull_request=True,
    )

    action.set_commit_status(
        github,
        context,
        action.GatingResult("failure", True, 2, [{"slice": "security"}]),
        target_url="https://app.test/diff",
    )

    assert "warning: could not set commit status" in capsys.readouterr().err
