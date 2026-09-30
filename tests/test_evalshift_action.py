from __future__ import annotations

import http.client
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evalshift_action as action
from _manifest import manifest_input_default


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


def test_action_config_requires_evalshift_version() -> None:
    """action.yml always supplies the version; an empty input means the script ran outside it."""
    with pytest.raises(action.ActionError, match="input 'evalshift-version' is required"):
        action.ActionConfig.from_env({"INPUT_TOKEN": "es_secret"})


def test_action_config_reads_evalshift_version_input() -> None:
    config = action.ActionConfig.from_env(
        {"INPUT_TOKEN": "es_secret", "INPUT_EVALSHIFT_VERSION": "0.13.1"}
    )

    assert config.evalshift_version == "0.13.1"


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


# The two ids a run has. `LOCAL_RUN_ID` names the directory under `.evalshift/runs` and is
# what `evalshift push` takes as its argument; `SERVER_RUN_ID` is what the server minted and
# what every `/runs/{id}` route addresses. Keeping both in the fixtures is the point — a test
# that used one string for both could not catch the two being swapped.
LOCAL_RUN_ID = "r_20260824_golden_ab12cd"
SERVER_RUN_ID = "3f6b1c2e-9a4d-4f11-8c0e-2b7d5a19e8c4"
# A second server id, for URLs that carry two — a diff route names both sides, and only the
# one behind `/runs/` is the run being gated.
OTHER_UUID = "8d21e7a0-5c63-4b98-9f04-6ae1cb37d250"
SERVER_RUN_URL = f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}"
# A project slug that is itself UUID-shaped. The server's `slugify` maps every run of
# non-alphanumerics to `-`, so a project named "3F6B1C2E 9A4D 4F11 8C0E 2B7D5A19E8C5" really
# does slugify to this. It differs from SERVER_RUN_ID only in the final character, so a parse
# that returns the slug instead of the run id shows up as a one-character assertion diff
# rather than as a plausible-looking pass.
UUID_SHAPED_SLUG = "3f6b1c2e-9a4d-4f11-8c0e-2b7d5a19e8c5"


def _fake_run_result() -> action.EvalShiftRunResult:
    """What `run_evalshift_commands` really returns, for tests that stub it out.

    Both ids are the realistic shapes: a stub that invented an id production can no longer
    produce would still assert green if `main()` started keying its hosted calls on the local
    run directory name again.
    """
    return action.EvalShiftRunResult(run_id=SERVER_RUN_ID, run_url=SERVER_RUN_URL)


def _push_config(**overrides: Any) -> action.ActionConfig:
    settings: dict[str, Any] = {
        "token": "es_secret",
        "host": "https://api.evalshift.dev",
        "config": "evalshift.yaml",
        "suite": "golden.jsonl",
        "suite_name": "",
        "evalshift_version": "0.4.0",
        "fail_on": "regression",
        "branch": "",
        "base_branch": "main",
        "create_project": False,
        "comment": True,
        "github_token": "ghs_secret",
    }
    settings.update(overrides)
    return action.ActionConfig(**settings)


def test_run_evalshift_commands_runs_all_then_push(tmp_path: Path) -> None:
    runs = tmp_path / ".evalshift" / "runs"
    (runs / LOCAL_RUN_ID).mkdir(parents=True)
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []
    run_url = f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}"

    def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> action.CommandResult:
        calls.append(cmd)
        envs.append(env)
        return action.CommandResult(
            stdout=f"{run_url}\n" if cmd[1] == "push" else "",
            returncode=0,
        )

    result = action.run_evalshift_commands(_push_config(), cwd=tmp_path, runner=fake_runner, env={})

    assert result.run_url == run_url
    # `all`, not `compare`: the alias is permanent, and it is the only spelling every
    # installable CLI version answers to. See run_evalshift_commands for the full reasoning.
    assert calls == [
        ["evalshift", "all", "--yes", "--config", "evalshift.yaml", "--suite", "golden.jsonl"],
        [
            "evalshift",
            "push",
            LOCAL_RUN_ID,
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


def _push_result(
    tmp_path: Path,
    push_stdout: str,
    *,
    envs: list[dict[str, str]] | None = None,
) -> action.EvalShiftRunResult:
    """Drive `run_evalshift_commands` with a canned `evalshift push` stdout."""
    (tmp_path / ".evalshift" / "runs" / LOCAL_RUN_ID).mkdir(parents=True)

    def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> action.CommandResult:
        if envs is not None:
            envs.append(env)
        return action.CommandResult(
            stdout=push_stdout if cmd[1] == "push" else "",
            returncode=0,
        )

    return action.run_evalshift_commands(_push_config(), cwd=tmp_path, runner=fake_runner, env={})


def test_run_evalshift_commands_reports_the_server_run_id_not_the_local_one(
    tmp_path: Path,
) -> None:
    """`/runs/{id}` takes the id the server minted, which the local directory name is not."""
    result = _push_result(
        tmp_path,
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}\n",
    )

    assert result.run_id == SERVER_RUN_ID


def test_run_evalshift_commands_rejects_a_push_url_with_no_server_run_id(
    tmp_path: Path,
) -> None:
    """Failing loudly beats gating on an id that 404s and reads as "no policy configured"."""
    with pytest.raises(action.ActionError, match="server run id"):
        _push_result(
            tmp_path,
            f"https://app.evalshift.dev/app/acme/project/runs/{LOCAL_RUN_ID}\n",
        )


def test_run_evalshift_commands_widens_the_cli_console(tmp_path: Path) -> None:
    """The CLI prints the hosted URL through Rich, which folds at the console width — 80 when
    stdout is a pipe, which it always is under `run_command`. A hosted run URL passes 80
    characters once the run id is a UUID, so the action asks for a width no URL will reach.
    """
    envs: list[dict[str, str]] = []
    _push_result(
        tmp_path,
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}\n",
        envs=envs,
    )

    assert envs
    for env in envs:
        # The exact value the action sets, not a range: a range is satisfied by whatever
        # `COLUMNS` the developer's own terminal happens to export, which made this test pass
        # with the production line deleted.
        assert env["COLUMNS"] == action.CLI_CONSOLE_COLUMNS


def _selected_commands(tmp_path: Path, config: action.ActionConfig) -> list[list[str]]:
    """Both CLI invocations `run_evalshift_commands` issues for ``config``."""
    (tmp_path / ".evalshift" / "runs" / LOCAL_RUN_ID).mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> action.CommandResult:
        calls.append(cmd)
        return action.CommandResult(
            stdout=f"{SERVER_RUN_URL}\n" if cmd[1] == "push" else "",
            returncode=0,
        )

    action.run_evalshift_commands(config, cwd=tmp_path, runner=fake_runner, env={})
    return calls


def test_a_named_suite_is_selected_by_name_on_both_commands(tmp_path: Path) -> None:
    """`--suite-name`, not `--suite`: a path loses the suite's own `evaluators:` block.

    A suite wired under `suites:` with its own tool evaluators, selected by path, is scored
    with the top-level evaluators instead — which for a tool-only suite means an empty
    `scores.jsonl` and a failed run that names no cause. `push` re-resolves the same
    selection to decide which evaluators the bundle claims, so both commands carry it.
    """
    calls = _selected_commands(tmp_path, _push_config(suite="", suite_name="planner"))

    assert calls == [
        ["evalshift", "all", "--yes", "--config", "evalshift.yaml", "--suite-name", "planner"],
        [
            "evalshift",
            "push",
            LOCAL_RUN_ID,
            "--config",
            "evalshift.yaml",
            "--suite-name",
            "planner",
            "--no-create-project",
        ],
    ]


def test_a_path_suite_still_selects_by_path(tmp_path: Path) -> None:
    """The `suite` input predates `suite-name` and keeps working untouched."""
    calls = _selected_commands(tmp_path, _push_config(suite="eval/golden.jsonl"))

    for cmd in calls:
        assert "--suite" in cmd
        assert "--suite-name" not in cmd
        assert cmd[cmd.index("--suite") + 1] == "eval/golden.jsonl"


def test_action_config_reads_the_suite_name_input() -> None:
    config = action.ActionConfig.from_env(
        {
            "INPUT_TOKEN": "es_secret",
            "INPUT_EVALSHIFT_VERSION": "1.0.0",
            "INPUT_SUITE_NAME": "planner",
        }
    )

    assert (config.suite, config.suite_name) == ("", "planner")


def test_action_config_defaults_to_the_bare_golden_suite() -> None:
    """Neither input set: the pre-`suite-name` default, unchanged."""
    config = action.ActionConfig.from_env(
        {"INPUT_TOKEN": "es_secret", "INPUT_EVALSHIFT_VERSION": "1.0.0"}
    )

    assert (config.suite, config.suite_name) == ("golden.jsonl", "")


def test_manifest_suite_default_is_resolved_by_the_script() -> None:
    """action.yml leaves `suite` empty so "both set" is detectable; the script fills it in."""
    assert manifest_input_default("suite") == ""
    assert manifest_input_default("suite-name") == ""


def test_action_config_rejects_both_suite_spellings() -> None:
    """Precedence would silently drop one selection — and each means something different."""
    with pytest.raises(action.ActionError, match="mutually exclusive"):
        action.ActionConfig.from_env(
            {
                "INPUT_TOKEN": "es_secret",
                "INPUT_EVALSHIFT_VERSION": "1.0.0",
                "INPUT_SUITE": "eval/golden.jsonl",
                "INPUT_SUITE_NAME": "planner",
            }
        )


def test_action_config_rejects_a_suite_name_on_a_pin_that_predates_it() -> None:
    """0.13.1 has no `--suite-name`; say so here instead of letting the CLI print a usage error."""
    with pytest.raises(action.ActionError, match=re.escape("0.14.0")):
        action.ActionConfig.from_env(
            {
                "INPUT_TOKEN": "es_secret",
                "INPUT_EVALSHIFT_VERSION": "0.13.1",
                "INPUT_SUITE_NAME": "planner",
            }
        )


def test_action_config_allows_a_suite_name_on_an_unparseable_pin() -> None:
    """A pin the guard cannot order (a local wheel, a git ref) is not evidence of an old CLI."""
    config = action.ActionConfig.from_env(
        {
            "INPUT_TOKEN": "es_secret",
            "INPUT_EVALSHIFT_VERSION": "main",
            "INPUT_SUITE_NAME": "planner",
        }
    )

    assert config.suite_name == "planner"


@pytest.mark.parametrize(
    "run_url",
    [
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}",
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}/",
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}?from=ci",
        # A deeper route under the same run: the id is the segment behind `/runs/`, not
        # whatever happens to end the path.
        f"https://app.evalshift.dev/app/acme/project/runs/{SERVER_RUN_ID}/diff/{OTHER_UUID}",
        # An org literally slugged `runs` whose project slug is UUID-shaped, so the path
        # carries two `/runs/<36 chars>` boundaries. Matching the leftmost one returned the
        # project slug — a valid-looking wrong UUID, which 404s on policy-check and degrades
        # to `fail-on: regression` without ever failing loudly. Anchoring the whole
        # `/app/{org}/{project}/runs/` shape is what makes the right one the leftmost.
        f"https://app.evalshift.dev/app/runs/{UUID_SHAPED_SLUG}/runs/{SERVER_RUN_ID}",
        # The same org slug without the colliding project slug: already correct before the
        # anchor widened, and pinned so it stays that way.
        f"https://app.evalshift.dev/app/runs/p/runs/{SERVER_RUN_ID}",
        # A deploy whose `web_app_url` carries a base path. `web_app_url` is a plain string
        # setting with no validator forbidding one, so the match is anchored but not rooted.
        f"https://evalshift.example.com/tools/app/acme/project/runs/{SERVER_RUN_ID}",
    ],
)
def test_server_run_id_from_url_reads_the_segment_behind_runs(run_url: str) -> None:
    assert action.server_run_id_from_url(run_url) == SERVER_RUN_ID


@pytest.mark.parametrize(
    "run_url",
    [
        # The client id — what the action used to pass to `/runs/{id}` and what a stale CLI
        # would still print in the URL.
        f"https://app.evalshift.dev/app/acme/project/runs/{LOCAL_RUN_ID}",
        # A URL Rich folded at 80 columns, so the id lost its tail.
        "https://app.evalshift.dev/app/acme-analytics/checkout-agent/runs/3f6b1c2e-9a4d-4",
        "https://app.evalshift.dev/app/acme/project",
        "",
        # A UUID that is not a run id. Shape alone is not identity: nothing but the segment
        # behind `/runs/` addresses a run, and a project id fed to `/runs/{id}` 404s exactly
        # like a missing policy decision.
        f"https://app.evalshift.dev/app/acme/projects/{SERVER_RUN_ID}",
        # A percent-encoded `/runs/` inside another segment. This one is the reason the parse
        # runs on the RAW path: unquoting first would decode this into a real `/runs/` boundary
        # and hand back an id that never sat behind one. Restoring `unquote()` turns this red.
        f"https://app.evalshift.dev/app/acme/project%2Fruns%2F{SERVER_RUN_ID}",
    ],
)
def test_server_run_id_from_url_refuses_anything_that_is_not_a_server_id(run_url: str) -> None:
    with pytest.raises(action.ActionError, match="server run id"):
        action.server_run_id_from_url(run_url)


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


def test_write_outputs_appends_key_value_lines(tmp_path: Path) -> None:
    output = tmp_path / "github_output"

    action.write_outputs(
        {"run_id": SERVER_RUN_ID, "conclusion": "success"},
        {"GITHUB_OUTPUT": str(output)},
    )

    assert output.read_text("utf-8") == f"run_id={SERVER_RUN_ID}\nconclusion=success\n"


def test_write_outputs_given_an_empty_env_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicitly empty env means "no GITHUB_OUTPUT", not "go look at the real one".

    Under a truthiness check this fell through to `os.environ`, where a real GitHub runner
    always has `GITHUB_OUTPUT` set — so a caller asking for no output would have appended to
    the live workflow's output file instead.
    """
    real_output = tmp_path / "real_github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(real_output))

    action.write_outputs({"run_id": SERVER_RUN_ID}, {})

    assert not real_output.exists()


def test_write_outputs_falls_back_to_the_process_env_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`None` is the real "I did not pass an env" signal, and `main()` relies on it."""
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    action.write_outputs({"conclusion": "failure"})

    assert output.read_text("utf-8") == "conclusion=failure\n"


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


def _policy_payload(status: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "status": status,
        "verdict": status,
        "reason": "pass-rate drop of 4.2 pts exceeds the allowed 2.0 pts",
        "policy_source": "project_policy",
        "policy": {"max_pass_rate_drop": 0.02},
        "budgets": [
            {
                "name": "pass_rate_drop",
                "observed": 0.042,
                "allowed": 0.02,
                "passed": False,
                "scope": "overall",
                "ci_low": 0.011,
                "ci_high": 0.073,
                "conclusive": True,
            },
            {
                "name": "cost_increase",
                "observed": 0.1,
                "allowed": 0.25,
                "passed": True,
                "scope": "overall",
                "ci_low": None,
                "ci_high": None,
                "conclusive": False,
            },
        ],
        "blocking_regressions": [
            {
                "prompt_id": "p-refund",
                "evaluator_name": "accuracy",
                "slice_name": "safety_refusals",
                "severity": "critical",
                "delta_avg_score": -0.4,
                "effect_size": -1.2,
            }
        ],
    }
    payload.update(overrides)
    return payload


CLEAN_DIFF: dict[str, Any] = {
    "aggregate_delta": {"regressions": 0, "pass_rate_delta": 0.0},
    "per_slice_deltas": [],
}
REGRESSED_DIFF: dict[str, Any] = {
    "aggregate_delta": {"regressions": 3, "pass_rate_delta": -0.2},
    "per_slice_deltas": [{"slice": "security", "pass_rate_delta": -0.5}],
}


def test_action_config_defaults_to_the_governed_policy_gate() -> None:
    config = action.ActionConfig.from_env(
        {"INPUT_TOKEN": "es_secret", "INPUT_EVALSHIFT_VERSION": "1.2.3"}
    )

    assert config.fail_on == "policy"


def test_manifest_fail_on_default_matches_the_script_default() -> None:
    assert manifest_input_default("fail-on") == "policy"


def test_action_config_rejects_an_unknown_fail_on_mode() -> None:
    with pytest.raises(action.ActionError) as excinfo:
        action.ActionConfig.from_env(
            {
                "INPUT_TOKEN": "es_secret",
                "INPUT_EVALSHIFT_VERSION": "1.2.3",
                "INPUT_FAIL_ON": "sometimes",
            }
        )

    message = str(excinfo.value)
    for mode in ("never", "regression", "any-slice-regression", "policy"):
        assert mode in message


def test_hosted_client_calls_the_policy_check_endpoint() -> None:
    requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> dict[str, Any]:
        requests.append((method, url, headers, data))
        return _policy_payload("fail")

    client = action.HostedClient("https://api.evalshift.dev/", "es_secret", request=fake_request)

    payload = client.policy_check("candidate")

    assert payload["status"] == "fail"
    method, url, headers, data = requests[0]
    assert method == "GET"
    assert url == "https://api.evalshift.dev/runs/candidate/policy-check"
    assert headers["Authorization"] == "Bearer es_secret"
    assert data is None


def test_policy_mode_fails_on_a_failing_policy_even_when_the_diff_is_clean() -> None:
    """The governed gate decides, not the diff — that is the whole point of `policy`."""
    result = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("fail"))

    assert result.should_fail is True
    assert result.conclusion == "failure"
    assert result.policy_status == "fail"
    assert result.policy_source == "project_policy"
    assert "exceeds the allowed" in result.policy_reason
    assert [budget["name"] for budget in result.budgets] == ["pass_rate_drop", "cost_increase"]
    assert result.blocking_regressions[0]["prompt_id"] == "p-refund"


def test_policy_mode_passes_on_a_passing_policy_even_when_the_diff_regressed() -> None:
    result = action.evaluate_gating(REGRESSED_DIFF, "policy", policy=_policy_payload("pass"))

    assert result.should_fail is False
    assert result.conclusion == "success"
    assert result.policy_status == "pass"
    # The diff facts still travel, because the comment still renders them.
    assert result.regression_count == 3
    assert result.top_slice_regressions[0]["slice"] == "security"


def test_policy_mode_does_not_render_an_inconclusive_decision_as_a_pass() -> None:
    result = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("inconclusive"))

    assert result.should_fail is False
    assert result.policy_status == "inconclusive"
    assert result.policy_decided is False
    assert "could not decide" in result.summary


# `policy_source: "none"` is the server saying the run carried no policy at all -- not that it
# weighed one and could not decide. The four tests below pin the difference, because the two
# arrive as the same `inconclusive` status and only the source tells a reader which one is theirs.
def test_an_ungated_run_says_the_gate_is_off_rather_than_undecided() -> None:
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="none"),
    )

    assert result.should_fail is False
    assert result.conclusion == "success"
    assert result.summary == (
        "the gate is off — no migration policy was pushed with this run; "
        "add migration_policy to evalshift.yaml"
    )


def test_an_ungated_run_prints_a_workflow_annotation_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GitHub reads annotations off the step's stdout only -- stderr is just log text."""
    action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="none"),
    )

    captured = capsys.readouterr()
    annotation = next(line for line in captured.out.splitlines() if line.startswith("::warning"))
    assert "migration_policy" in annotation
    assert "evalshift.yaml" in annotation
    assert "::warning" not in captured.err


def test_the_ungated_summary_does_not_repeat_the_server_reason() -> None:
    """The fix is already in the sentence; the reason still travels to the PR comment."""
    reason = "No migration policy was pushed with this run."
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="none", reason=reason),
    )

    assert reason not in result.summary
    assert result.policy_reason == reason


def test_a_run_that_carried_a_policy_is_never_annotated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="run_policy"),
    )

    assert "could not decide" in result.summary
    assert "::warning" not in capsys.readouterr().out


def test_require_policy_fails_an_ungated_run() -> None:
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="none"),
        require_policy=True,
    )

    assert result.should_fail is True
    assert result.conclusion == "failure"
    assert "require-policy" in result.summary
    assert "add migration_policy to evalshift.yaml" in result.summary


def test_require_policy_leaves_a_run_that_carried_a_policy_alone() -> None:
    """It is an opt-in about the missing policy, not a second opinion on the verdict."""
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="run_policy"),
        require_policy=True,
    )

    assert result.should_fail is False
    assert result.conclusion == "success"


def test_require_policy_does_not_fail_the_unreachable_policy_check() -> None:
    """No decision at all is the fallback path's business; require-policy has no opinion there."""
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=None,
        policy_unavailable="hosted policy check returned HTTP 503",
        require_policy=True,
    )

    assert result.should_fail is False
    assert "fell back to fail-on" in result.summary


def test_action_config_does_not_require_a_policy_by_default() -> None:
    config = action.ActionConfig.from_env(
        {"INPUT_TOKEN": "es_secret", "INPUT_EVALSHIFT_VERSION": "1.2.3"}
    )

    assert config.require_policy is False


def test_action_config_reads_the_require_policy_input() -> None:
    config = action.ActionConfig.from_env(
        {
            "INPUT_TOKEN": "es_secret",
            "INPUT_EVALSHIFT_VERSION": "1.2.3",
            "INPUT_REQUIRE_POLICY": "true",
        }
    )

    assert config.require_policy is True


def test_manifest_require_policy_default_matches_the_script_default() -> None:
    assert manifest_input_default("require-policy") == "false"


def test_every_input_the_script_reads_is_wired_into_the_step_env() -> None:
    """An input read here but not passed through action.yml is silently always its default."""
    manifest = Path(__file__).resolve().parents[1] / "action.yml"
    script = (Path(__file__).resolve().parents[1] / "scripts" / "evalshift_action.py").read_text(
        encoding="utf-8"
    )
    wired = manifest.read_text(encoding="utf-8")
    read = set(re.findall(r'_(?:bool_)?input\(\s*source,\s*"([A-Z_]+)"', script))

    assert read
    assert [name for name in sorted(read) if f"INPUT_{name}:" not in wired] == []


def test_policy_mode_survives_a_status_it_has_never_heard_of() -> None:
    """The server's status vocabulary grows; an older action must not crash or call it green."""
    result = action.evaluate_gating(
        CLEAN_DIFF, "policy", policy=_policy_payload("quantum_superposition")
    )

    assert result.should_fail is False
    assert result.policy_decided is False
    assert "quantum_superposition" in result.summary
    assert "unrecognized" in result.summary


# The server's own wording for `conditional_pass`: it says out loud that it is not a gate
# failure, and it ends by telling the reader to look anyway.
CONDITIONAL_PASS_REASON = (
    "2 medium regressions and 1 comparison that scored zero pairs; every budget held and "
    "nothing critical or high regressed, so this is not a gate failure. Review before merging."
)


def test_policy_mode_treats_conditional_pass_as_a_decided_pass_with_caveats() -> None:
    """`conditional_pass` is a recognized, passing verdict — not an unknown, not undecided."""
    result = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("conditional_pass", reason=CONDITIONAL_PASS_REASON),
    )

    assert result.should_fail is False
    assert result.conclusion == "success"
    assert result.policy_status == "conditional_pass"
    assert result.policy_decided is True
    assert result.policy_caveated is True
    assert "unrecognized" not in result.summary
    assert "could not decide" not in result.summary
    assert "caveat" in result.summary
    assert CONDITIONAL_PASS_REASON in result.summary


def test_policy_mode_keeps_a_plain_pass_free_of_caveats() -> None:
    result = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("pass"))

    assert result.policy_decided is True
    assert result.policy_caveated is False
    assert "caveat" not in result.summary


def test_policy_mode_falls_back_to_regression_gating_and_says_so() -> None:
    result = action.evaluate_gating(
        REGRESSED_DIFF,
        "policy",
        policy=None,
        policy_unavailable="hosted policy check returned HTTP 500",
    )

    assert result.should_fail is True
    assert result.conclusion == "failure"
    assert result.policy_unavailable_reason == "hosted policy check returned HTTP 500"
    assert "fell back" in result.summary
    assert "regression" in result.summary


def test_policy_fallback_does_not_invent_a_failure_from_a_clean_diff() -> None:
    result = action.evaluate_gating(
        CLEAN_DIFF, "policy", policy=None, policy_unavailable="no stored decision"
    )

    assert result.should_fail is False
    assert result.policy_unavailable_reason == "no stored decision"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (HTTPError("https://api.test/policy-check", 404, "Not Found", {}, None), "404"),
        (HTTPError("https://api.test/policy-check", 500, "Server Error", {}, None), "500"),
        (URLError("connection refused"), "connection refused"),
        (action.ActionError("hosted EvalShift refused the request (HTTP 403)"), "403"),
    ],
)
def test_fetch_policy_check_reports_a_failure_instead_of_swallowing_it(
    failure: Exception,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenClient:
        def policy_check(self, run_id: str) -> dict[str, Any]:
            raise failure

    payload, reason = action.fetch_policy_check(BrokenClient(), "run-1")

    assert payload is None
    assert expected in reason
    assert reason in capsys.readouterr().err


def test_fetch_policy_check_names_a_missing_policy_read_in_its_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A key without `policy:read` degrades the default gate; the warning must say which scope."""

    def forbidden_request(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError(
            "https://api.evalshift.test/runs/r/policy-check",
            403,
            "Forbidden",
            {},
            io.BytesIO(_error_envelope("forbidden", "Permission denied: policy:read")),
        )

    client = action.HostedClient(
        "https://api.evalshift.test", "es_secret", request=forbidden_request
    )

    payload, reason = action.fetch_policy_check(client, SERVER_RUN_ID)

    assert payload is None
    assert "HTTP 403" in reason
    assert "'policy:read'" in reason
    assert "falling back to fail-on: regression" in capsys.readouterr().err


def test_fetch_policy_check_treats_a_decisionless_response_as_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class SilentClient:
        def policy_check(self, run_id: str) -> dict[str, Any]:
            return {"run_id": run_id}

    payload, reason = action.fetch_policy_check(SilentClient(), "run-1")

    assert payload is None
    assert "no decision" in reason
    assert "warning" in capsys.readouterr().err


def test_fetch_policy_check_returns_the_decision_when_the_server_has_one() -> None:
    class HealthyClient:
        def policy_check(self, run_id: str) -> dict[str, Any]:
            return _policy_payload("fail")

    payload, reason = action.fetch_policy_check(HealthyClient(), "run-1")

    assert payload is not None
    assert payload["status"] == "fail"
    assert reason == ""


def _policy_comment(gating: action.GatingResult, diff: dict[str, Any] | None = None) -> str:
    return action.build_comment_body(
        run_url="https://app.test/run",
        diff_url="https://app.test/diff" if diff else None,
        baseline={"id": "base"} if diff else None,
        diff=diff,
        gating=gating,
    )


def test_comment_renders_the_policy_decision_budgets_and_blocking_regressions() -> None:
    gating = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("fail"))

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "project_policy" in body
    assert "pass-rate drop of 4.2 pts exceeds the allowed 2.0 pts" in body
    assert "pass_rate_drop" in body
    assert "overall" in body
    assert "0.042" in body
    assert "0.02" in body
    # The confidence-free budget is flagged rather than shown as a clean pass.
    assert "not confident" in body
    assert "p-refund" in body
    assert "safety_refusals" in body
    assert "critical" in body
    # The existing diff section survives.
    assert "| Slice | Pass-rate delta |" in body


def test_comment_says_plainly_when_the_policy_could_not_decide() -> None:
    gating = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("inconclusive"))

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "could not decide" in body
    assert "`inconclusive`" in body


def test_comment_renders_conditional_pass_as_a_pass_that_still_needs_a_look() -> None:
    """It is a pass, so it must not read as undecided — and it has a caveat, so not as clean."""
    gating = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("conditional_pass", reason=CONDITIONAL_PASS_REASON),
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "`conditional_pass`" in body
    assert CONDITIONAL_PASS_REASON in body
    assert "passed, with caveats" in body
    # The undecided blockquote belongs to `inconclusive` and to unknown statuses only.
    assert "could not decide" not in body
    assert "unrecognized" not in body


def test_comment_does_not_caveat_a_plain_pass() -> None:
    gating = action.evaluate_gating(CLEAN_DIFF, "policy", policy=_policy_payload("pass"))

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "`pass`" in body
    assert "caveat" not in body
    assert "could not decide" not in body


def test_comment_says_nothing_gates_the_pr_when_no_policy_was_pushed() -> None:
    """An absent policy is not a policy that reached no verdict; the comment must not blur them."""
    gating = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", policy_source="none", budgets=[]),
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "Nothing gates this PR" in body
    assert "migration_policy" in body
    assert "reached no verdict" not in body


def test_comment_announces_a_policy_fallback() -> None:
    gating = action.evaluate_gating(
        CLEAN_DIFF, "policy", policy=None, policy_unavailable="hosted policy check returned 500"
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "hosted policy check returned 500" in body
    assert "fail-on: regression" in body


def test_comment_caps_the_budget_table_and_keeps_the_failing_rows() -> None:
    budgets = [
        {
            "name": f"budget_{index}",
            "observed": 0.1,
            "allowed": 0.5,
            "passed": True,
            "scope": "slice:s{index}",
            "conclusive": True,
        }
        for index in range(40)
    ]
    budgets.append(
        {
            "name": "the_one_that_failed",
            "observed": 0.9,
            "allowed": 0.1,
            "passed": False,
            "scope": "overall",
            "conclusive": True,
        }
    )
    gating = action.evaluate_gating(
        CLEAN_DIFF, "policy", policy=_policy_payload("fail", budgets=budgets)
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert body.count("| budget_") <= action.MAX_BUDGET_ROWS
    assert "the_one_that_failed" in body
    assert "more budgets not shown" in body
    # Nothing failing was hidden here, so the cap line must not imply otherwise.
    assert "of them failing" not in body


def _slice_scoped_budgets() -> list[dict[str, Any]]:
    """What P2 made possible: one budget name, evaluated per slice, more than six rows."""
    return [
        {
            "name": "pass_rate_drop",
            "observed": observed,
            "allowed": 0.02,
            "passed": observed <= 0.02,
            "scope": scope,
            "ci_low": None,
            "ci_high": None,
            "conclusive": True,
        }
        for scope, observed in (
            ("overall", 0.01),
            ("billing", 0.005),
            ("safety_refusals", 0.09),
            ("tool_use", 0.0),
            ("long_context", 0.011),
            ("multilingual", 0.002),
            ("summarization", 0.004),
        )
    ]


def test_comment_names_the_slice_each_budget_row_was_scoped_to() -> None:
    """A slice-scoped budget is unreadable unless the row says which slice it judged."""
    gating = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("fail", budgets=_slice_scoped_budgets()),
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    # Seven rows share one budget name; only `scope` tells them apart.
    for scope in ("overall", "billing", "safety_refusals", "tool_use", "multilingual"):
        assert f"| pass_rate_drop | {scope} |" in body
    assert "| pass_rate_drop | safety_refusals | 0.09 | 0.02 | fail |" in body


def test_comment_cap_line_admits_when_it_is_hiding_failing_budgets() -> None:
    """Over the cap, "N more budgets not shown" must not bury N failures under a neutral line."""
    budgets = [
        {
            "name": "pass_rate_drop",
            "observed": 0.9,
            "allowed": 0.02,
            "passed": False,
            "scope": f"slice_{index}",
            "conclusive": True,
        }
        for index in range(action.MAX_BUDGET_ROWS + 8)
    ]
    gating = action.evaluate_gating(
        CLEAN_DIFF, "policy", policy=_policy_payload("fail", budgets=budgets)
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert body.count("| pass_rate_drop |") == action.MAX_BUDGET_ROWS
    assert "8 more budgets not shown" in body
    assert "8 of them failing" in body


def test_comment_renders_no_budget_table_for_an_all_insufficient_run() -> None:
    """P3 returns `budgets: []` with `inconclusive`; an empty table would be worse than none."""
    reason = "No comparable results: all 14 comparisons scored severity 'insufficient'."
    gating = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", reason=reason, budgets=[], blocking_regressions=[]),
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert "Policy budgets" not in body
    assert "| Budget | Scope |" not in body
    assert "Blocking regressions" not in body
    # The reason is the whole of what the reader gets, so it had better be there.
    assert reason in body
    assert "could not decide" in body


INCONCLUSIVE_REASONS = (
    "No policy metrics recorded for this run.",
    "No comparable results: all 14 comparisons scored severity 'insufficient'.",
    "Policy slice 'safety_refusals' not measured by this run.",
)


@pytest.mark.parametrize("reason", INCONCLUSIVE_REASONS)
def test_comment_surfaces_each_inconclusive_reason_verbatim(reason: str) -> None:
    """Three causes ride on the reason string alone; generic wording would collapse them."""
    gating = action.evaluate_gating(
        CLEAN_DIFF,
        "policy",
        policy=_policy_payload("inconclusive", reason=reason, budgets=[]),
    )

    body = _policy_comment(gating, CLEAN_DIFF)

    assert reason in body


def test_the_three_inconclusive_causes_produce_three_different_comments() -> None:
    bodies = {
        _policy_comment(
            action.evaluate_gating(
                CLEAN_DIFF,
                "policy",
                policy=_policy_payload("inconclusive", reason=reason, budgets=[]),
            ),
            CLEAN_DIFF,
        )
        for reason in INCONCLUSIVE_REASONS
    }

    assert len(bodies) == len(INCONCLUSIVE_REASONS)


def test_comment_renders_a_policy_decision_without_a_baseline() -> None:
    gating = action.evaluate_gating(None, "policy", policy=_policy_payload("fail"))

    body = _policy_comment(gating)

    assert "No compatible baseline run was found" in body
    assert "pass_rate_drop" in body
    assert "p-refund" in body


def test_comment_escapes_a_pipe_in_a_slice_name() -> None:
    diff: dict[str, Any] = {
        "aggregate_delta": {"regressions": 1, "pass_rate_delta": -0.2},
        "per_slice_deltas": [{"slice": "billing | refunds", "pass_rate_delta": -0.5}],
    }
    gating = action.evaluate_gating(diff, "regression")

    body = _policy_comment(gating, diff)

    assert "| billing \\| refunds | -50 pts |" in body


def test_comment_is_unchanged_for_the_diff_only_modes() -> None:
    gating = action.evaluate_gating(REGRESSED_DIFF, "regression")

    body = _policy_comment(gating, REGRESSED_DIFF)

    assert "Policy" not in body
    assert "budget" not in body


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


def test_missing_permission_hint_names_the_denied_permission() -> None:
    body = '{"error": {"code": "forbidden", "message": "Permission denied: run:create"}}'

    hint = action.missing_permission_hint(body)

    assert hint is not None
    assert "run:create" in hint
    assert "service-account key" in hint


def test_missing_permission_hint_ignores_unrelated_output() -> None:
    assert action.missing_permission_hint("evalshift: suite golden.jsonl not found") is None


def test_hosted_client_turns_403_into_a_self_diagnosing_error() -> None:
    def forbidden_request(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError(
            "https://api.evalshift.test/runs/candidate/baseline-compatible",
            403,
            "Forbidden",
            {},
            io.BytesIO(
                b'{"error": {"code": "forbidden", "message": "Permission denied: run:read"}}'
            ),
        )

    client = action.HostedClient(
        "https://api.evalshift.test", "es_secret", request=forbidden_request
    )

    with pytest.raises(action.ActionError) as excinfo:
        client.baseline_compatible("candidate", "main")

    message = str(excinfo.value)
    assert "403" in message
    assert "run:read" in message
    assert "service-account key" in message


def test_hosted_client_leaves_other_http_errors_alone() -> None:
    def failing_request(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError("https://api.evalshift.test/runs", 500, "Server Error", {}, None)

    client = action.HostedClient("https://api.evalshift.test", "es_secret", request=failing_request)

    with pytest.raises(HTTPError):
        client.run_diff("/runs/base/diff/candidate")


def test_run_command_failure_explains_a_cli_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Completed:
        stdout = ""
        stderr = "✗ Permission denied: run:create\n"
        returncode = 1

    def fake_run(*args: Any, **kwargs: Any) -> Completed:
        return Completed()

    monkeypatch.setattr(action.subprocess, "run", fake_run)

    with pytest.raises(action.ActionError) as excinfo:
        action.run_command(["evalshift", "push", "run-1"], tmp_path, {})

    message = str(excinfo.value)
    assert "command failed (1)" in message
    assert "run:create" in message
    assert "service-account key" in message


DENIAL_BODY = json.dumps(
    {
        "error": {
            "code": "payment_required",
            "message": "Private-repo CI is not included in the Free plan.",
            "details": {
                "feature": "private_repo_ci",
                "limit": None,
                "used": None,
                "tier": "free",
                "status": "active",
                "resets_at": None,
                "upgrade_url": "https://app.evalshift.dev/app/acme/settings/billing",
            },
        }
    }
).encode("utf-8")


PREFLIGHT_URL = "https://api.evalshift.test/runs/preflight"


def _http_error(code: int, body: bytes | None = None) -> HTTPError:
    return HTTPError(
        PREFLIGHT_URL,
        code,
        f"HTTP {code}",
        {},
        io.BytesIO(body) if body is not None else None,
    )


def _error_envelope(code: str, message: str) -> bytes:
    """The hosted error envelope every non-2xx response carries (app/core/errors.py)."""
    return json.dumps({"error": {"code": code, "message": message, "details": None}}).encode()


RequestLog = list[tuple[str, str, dict[str, str], bytes | None]]


def _preflight_client(outcome: Any) -> tuple[action.HostedClient, RequestLog]:
    """A real ``HostedClient`` whose transport answers every request with ``outcome``.

    ``outcome`` is either a response body to return or an exception to raise. Every request is
    logged, so a test can assert on how many calls were made and to where -- the preflight is
    one call, and a second one (a fallback, a project lookup) is exactly what must not happen.
    """
    requests: RequestLog = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> Any:
        requests.append((method, url, headers, data))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client = action.HostedClient("https://api.evalshift.test", "es_secret", request=fake_request)
    return client, requests


def test_the_preflight_is_one_post_to_runs_preflight_addressed_by_slug() -> None:
    """One call, by slug, with the key the upload uses -- no project lookup in front of it.

    The old flow listed `/orgs/{org}/projects` first, which needs `project:read`: the
    documented CI key got 403 there, a project-pinned key got 404, and either way the check
    that followed never ran.
    """
    client, requests = _preflight_client({"allowed": True})

    result = action.run_preflight(
        client,
        project_ref=("acme", "checkout"),
        repo_private=True,
        create_project=False,
    )

    assert result is None
    assert len(requests) == 1
    method, url, headers, data = requests[0]
    assert method == "POST"
    assert url == PREFLIGHT_URL
    assert headers["Authorization"] == "Bearer es_secret"
    assert data is not None
    assert json.loads(data) == {
        "project_slug": "acme/checkout",
        "repo_private": True,
        "parallelism": action.PREFLIGHT_PARALLELISM,
    }
    assert not any("/orgs/" in logged_url for _, logged_url, _, _ in requests)


def test_ci_preflight_402_carries_the_servers_message_and_details() -> None:
    client, _ = _preflight_client(_http_error(402, DENIAL_BODY))

    with pytest.raises(action.PreflightDenied) as excinfo:
        client.ci_preflight("acme/checkout", repo_private=True)

    denial = excinfo.value
    assert denial.message == "Private-repo CI is not included in the Free plan."
    assert denial.details["feature"] == "private_repo_ci"
    assert denial.details["tier"] == "free"


def test_run_preflight_returns_the_denial_on_402() -> None:
    client, _ = _preflight_client(_http_error(402, DENIAL_BODY))

    denial = action.run_preflight(
        client, project_ref=("acme", "checkout"), repo_private=True, create_project=True
    )

    assert denial is not None
    assert denial.details["feature"] == "private_repo_ci"


@pytest.mark.parametrize("create_project", [True, False])
def test_run_preflight_stops_on_a_rejected_token(
    create_project: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 401 will be a 401 at push time too -- after the whole suite has been paid for."""
    client, _ = _preflight_client(
        _http_error(401, _error_envelope("unauthorized", "Invalid API token"))
    )

    with pytest.raises(action.ActionError) as excinfo:
        action.run_preflight(
            client,
            project_ref=("acme", "checkout"),
            repo_private=False,
            create_project=create_project,
        )

    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "Invalid API token" in message
    assert "https://api.evalshift.test" in message
    assert "service-account key" in message
    assert "plan preflight skipped" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        _error_envelope("forbidden", "Permission denied: run:create"),
        None,
    ],
    ids=["server-names-the-permission", "no-body"],
)
def test_run_preflight_stops_on_a_key_without_run_create(body: bytes | None) -> None:
    """The route's only permission is `run:create`, the same one `evalshift push` needs."""
    client, _ = _preflight_client(_http_error(403, body))

    with pytest.raises(action.ActionError) as excinfo:
        action.run_preflight(
            client, project_ref=("acme", "checkout"), repo_private=False, create_project=True
        )

    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "'run:create'" in message
    assert "service-account key" in message


def test_run_preflight_stops_on_a_missing_project_when_push_may_not_create_it() -> None:
    """With `create-project: false` the push would fail on this same 404, after the suite."""
    client, _ = _preflight_client(_http_error(404, _error_envelope("not_found", "Not found")))

    with pytest.raises(action.ActionError) as excinfo:
        action.run_preflight(
            client, project_ref=("acme", "checkout"), repo_private=False, create_project=False
        )

    message = str(excinfo.value)
    assert "HTTP 404" in message
    assert "acme/checkout" in message
    assert "create-project: false" in message


def test_run_preflight_notices_and_continues_when_the_first_push_will_create_the_project(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, requests = _preflight_client(_http_error(404))

    result = action.run_preflight(
        client, project_ref=("acme", "checkout"), repo_private=False, create_project=True
    )

    captured = capsys.readouterr()
    assert result is None
    assert len(requests) == 1
    # A workflow command, so stdout -- GitHub only scrapes those off the step's stdout.
    assert captured.out.startswith("::notice title=EvalShift preflight::")
    assert "acme/checkout" in captured.out
    assert "first push creates it" in captured.out
    assert "\n" not in captured.out.rstrip("\n")


def test_run_preflight_warns_and_continues_on_a_server_that_predates_the_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An older server answers 405 (`/runs/{run_id}` matches the path, not the method).

    No fallback to the old two-call flow: it never worked with the documented key anyway.
    """
    client, requests = _preflight_client(
        _http_error(405, _error_envelope("validation_error", "Method Not Allowed"))
    )

    result = action.run_preflight(
        client, project_ref=("acme", "checkout"), repo_private=True, create_project=False
    )

    assert result is None
    assert len(requests) == 1
    assert "predates POST /runs/preflight" in capsys.readouterr().err


@pytest.mark.parametrize("create_project", [True, False])
@pytest.mark.parametrize(
    "failure",
    [
        _http_error(500),
        _http_error(502),
        _http_error(503),
        _http_error(422, _error_envelope("validation_error", "Request validation failed")),
        _http_error(429),
        URLError("connection refused"),
        TimeoutError("timed out"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
        # Neither OSError nor ValueError: a garbled status line, a body cut short.
        http.client.BadStatusLine("garbage"),
        http.client.IncompleteRead(b"{", 10),
    ],
    ids=[
        "500",
        "502",
        "503",
        "422",
        "429",
        "url-error",
        "timeout",
        "malformed-body",
        "bad-status-line",
        "incomplete-read",
    ],
)
def test_run_preflight_never_blocks_on_an_infrastructure_failure(
    failure: Exception,
    create_project: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail-open on infrastructure: an EvalShift outage must not break every customer's CI."""
    client, _ = _preflight_client(failure)

    result = action.run_preflight(
        client,
        project_ref=("acme", "checkout"),
        repo_private=True,
        create_project=create_project,
    )

    assert result is None
    assert "warning: plan preflight skipped:" in capsys.readouterr().err


def test_run_preflight_is_skipped_without_a_project_ref() -> None:
    client, requests = _preflight_client(AssertionError("no project ref, nothing to ask"))

    assert (
        action.run_preflight(client, project_ref=None, repo_private=True, create_project=True)
        is None
    )
    assert requests == []


def test_project_ref_from_config_reads_the_project_key(tmp_path: Path) -> None:
    config = tmp_path / "evalshift.yaml"
    config.write_text('version: 1\nproject: "acme/checkout"\nprompts: []\n', encoding="utf-8")

    assert action.project_ref_from_config(config) == ("acme", "checkout")


def test_project_ref_from_config_ignores_a_nested_project_key(tmp_path: Path) -> None:
    """Only the top-level `project:` names the hosted project; an indented one is something else."""
    config = tmp_path / "evalshift.yaml"
    config.write_text("version: 1\ndefaults:\n  project: acme/nested\n", encoding="utf-8")

    assert action.project_ref_from_config(config) is None


def test_project_ref_from_config_returns_none_when_the_file_is_missing(tmp_path: Path) -> None:
    assert action.project_ref_from_config(tmp_path / "nope.yaml") is None


def test_preflight_body_names_the_plan_the_block_and_the_upgrade_url() -> None:
    denial = action.PreflightDenied(
        "Private-repo CI is not included in the Free plan.",
        {
            "feature": "private_repo_ci",
            "tier": "free",
            "limit": None,
            "used": None,
            "resets_at": None,
            "upgrade_url": "https://app.evalshift.dev/app/acme/settings/billing",
        },
    )

    body = action.build_preflight_body(denial)

    assert action.COMMENT_MARKER in body
    assert "Private-repo CI is not included in the Free plan." in body
    assert "free" in body
    assert "private_repo_ci" in body
    assert "https://app.evalshift.dev/app/acme/settings/billing" in body


def test_preflight_body_reports_a_quota_limit_and_its_reset_date() -> None:
    denial = action.PreflightDenied(
        "This organization has used all 100 runs in its plan.",
        {
            "feature": "runs_per_month",
            "tier": "free",
            "limit": 100,
            "used": 100,
            "resets_at": "2026-08-01",
            "upgrade_url": "https://app.evalshift.dev/app/acme/settings/billing",
        },
    )

    body = action.build_preflight_body(denial)

    assert "100" in body
    assert "2026-08-01" in body


def test_error_annotation_is_a_single_line() -> None:
    annotation = action.error_annotation("blocked\nupgrade here")

    assert annotation.startswith("::error title=EvalShift::")
    assert "\n" not in annotation
    assert "%0A" in annotation


def test_workflow_command_escapes_percent_before_the_line_breaks() -> None:
    """`%` first, or the `%` of an escaped `%0D` would itself be escaped to `%250D`."""
    assert action.workflow_command("notice", "T", "50%\r\nx") == "::notice title=T::50%25%0D%0Ax"


def test_workflow_command_escapes_the_title_property() -> None:
    """A property value also ends at `:` or `,`, so those escape too -- in the title only."""
    command = action.workflow_command("notice", "a:b,c%d\r\ne", "k:v,w")

    assert command == "::notice title=a%3Ab%2Cc%25d%0D%0Ae::k:v,w"


def _preflight_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    preflight: Any,
    env: dict[str, str] | None = None,
) -> tuple[int, RequestLog, list[bool]]:
    """Run ``main`` against a real ``HostedClient`` whose preflight answers with ``preflight``.

    Only the transport is faked, so the request the action really builds is what gets logged.
    Every request after the preflight is the post-push baseline lookup and policy check, which
    answer as if the run had no baseline and passed its policy.
    """
    _preflight_workspace(tmp_path)
    requests: RequestLog = []
    ran: list[bool] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> Any:
        requests.append((method, url, headers, data))
        if url.endswith("/runs/preflight"):
            if isinstance(preflight, BaseException):
                raise preflight
            return preflight
        if url.endswith("/policy-check"):
            return _policy_payload("pass")
        return {}

    def fake_run(*args: Any, **kwargs: Any) -> action.EvalShiftRunResult:
        ran.append(True)
        return _fake_run_result()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(action, "http_request", fake_request)
    monkeypatch.setattr(action, "run_evalshift_commands", fake_run)
    for key in list(os.environ):
        if key.startswith(("INPUT_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_TOKEN", "es_secret")
    monkeypatch.setenv("INPUT_HOST", "https://api.evalshift.test")
    monkeypatch.setenv("INPUT_EVALSHIFT_VERSION", "1.2.3")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return action.main(), requests, ran


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text("utf-8").splitlines())


STOPPED_OUTPUTS = {
    "run_url": "",
    "diff_url": "",
    "run_id": "",
    "regression_count": "0",
    "conclusion": "failure",
}


def _preflight_workspace(tmp_path: Path) -> None:
    (tmp_path / "evalshift.yaml").write_text("version: 1\nproject: acme/checkout\n", "utf-8")


def test_a_denied_preflight_fails_the_job_without_running_the_suite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = tmp_path / "summary.md"
    outputs = tmp_path / "outputs.txt"

    exit_code, requests, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight=_http_error(402, DENIAL_BODY),
        env={
            "INPUT_REPO_PRIVATE": "true",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(outputs),
        },
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert ran == []
    assert [url for _, url, _, _ in requests] == [PREFLIGHT_URL]
    assert json.loads(requests[0][3] or b"")["repo_private"] is True
    assert "::error title=EvalShift::" in captured.out
    assert "Private-repo CI is not included in the Free plan." in captured.out
    assert "https://app.evalshift.dev/app/acme/settings/billing" in summary.read_text("utf-8")
    assert _outputs(outputs) == STOPPED_OUTPUTS


def test_an_allowed_preflight_lets_the_suite_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exit_code, requests, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight={"allowed": True},
        env={"INPUT_REPO_PRIVATE": "false"},
    )

    assert exit_code == 0
    assert ran == [True]
    assert requests[0][1] == PREFLIGHT_URL
    assert json.loads(requests[0][3] or b"") == {
        "project_slug": "acme/checkout",
        "repo_private": False,
        "parallelism": 1,
    }


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (401, _error_envelope("unauthorized", "Invalid API token"), "HTTP 401"),
        (403, _error_envelope("forbidden", "Permission denied: run:create"), "'run:create'"),
    ],
    ids=["401", "403"],
)
def test_a_preflight_the_token_cannot_pass_stops_the_job_before_the_suite(
    code: int,
    body: bytes,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The push would fail the same way -- after every model call in the suite was paid for."""
    outputs = tmp_path / "outputs.txt"
    summary = tmp_path / "summary.md"

    exit_code, requests, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight=_http_error(code, body),
        env={"GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary)},
    )

    assert exit_code == 1
    assert ran == []
    assert len(requests) == 1
    annotation = _single_error_annotation(capsys.readouterr())
    assert expected in annotation
    # The fix is on the line after the status: kept, escaped, inside the one annotation.
    assert "%0A" in annotation
    assert expected in summary.read_text("utf-8")
    assert _outputs(outputs) == STOPPED_OUTPUTS


def _single_error_annotation(captured: pytest.CaptureResult[str]) -> str:
    """The one ``::error::`` line a stopped job prints, asserting it was printed only once."""
    annotations = [
        line for line in captured.out.splitlines() if line.startswith("::error title=EvalShift::")
    ]
    assert len(annotations) == 1, captured.out
    # Printed as an annotation, not repeated as a plain `error:` line by the outer handler.
    assert "error:" not in captured.err
    return annotations[0]


def test_a_missing_project_stops_the_job_when_create_project_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outputs = tmp_path / "outputs.txt"

    summary = tmp_path / "summary.md"

    exit_code, _, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight=_http_error(404),
        env={
            "INPUT_CREATE_PROJECT": "false",
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )

    assert exit_code == 1
    assert ran == []
    assert "acme/checkout" in _single_error_annotation(capsys.readouterr())
    assert "## EvalShift did not run" in summary.read_text("utf-8")
    assert _outputs(outputs) == STOPPED_OUTPUTS


def test_a_missing_project_is_a_notice_when_the_first_push_creates_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, _, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight=_http_error(404),
        env={"INPUT_CREATE_PROJECT": "true"},
    )

    assert exit_code == 0
    assert ran == [True]
    assert "::notice title=EvalShift preflight::" in capsys.readouterr().out


def test_an_older_server_lets_the_suite_run_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, requests, ran = _preflight_main(
        monkeypatch,
        tmp_path,
        preflight=_http_error(405),
    )

    assert exit_code == 0
    assert ran == [True]
    assert [url for _, url, _, _ in requests].count(PREFLIGHT_URL) == 1
    assert not any("/orgs/" in url or "ci-preflight" in url for _, url, _, _ in requests)
    assert "predates POST /runs/preflight" in capsys.readouterr().err


def test_repo_private_input_defaults_to_the_github_context() -> None:
    assert manifest_input_default("repo-private") == "${{ github.event.repository.private }}"


class FakePolicyHostedClient:
    """Stands in for ``HostedClient`` in ``main``: an allowed preflight, one policy decision."""

    payload: ClassVar[dict[str, Any]] = {}
    failure: ClassVar[Exception | None] = None
    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.token = token

    def ci_preflight(self, project_slug: str, *, repo_private: bool) -> None:
        return None

    def baseline_compatible(self, run_id: str, branch: str) -> dict[str, Any]:
        return {}

    def run_diff(self, api_diff_url: str) -> dict[str, Any]:
        return {}

    def policy_check(self, run_id: str) -> dict[str, Any]:
        type(self).calls.append(run_id)
        if type(self).failure is not None:
            raise type(self).failure
        return dict(type(self).payload)


def _policy_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    payload: dict[str, Any],
    failure: Exception | None = None,
    env: dict[str, str] | None = None,
) -> int:
    _preflight_workspace(tmp_path)
    FakePolicyHostedClient.payload = payload
    FakePolicyHostedClient.failure = failure
    FakePolicyHostedClient.calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(action, "HostedClient", FakePolicyHostedClient)
    monkeypatch.setattr(
        action,
        "run_evalshift_commands",
        lambda *args, **kwargs: _fake_run_result(),
    )
    for key in list(os.environ):
        if key.startswith(("INPUT_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_TOKEN", "es_secret")
    monkeypatch.setenv("INPUT_EVALSHIFT_VERSION", "1.2.3")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return action.main()


def test_main_fails_the_job_on_a_failing_policy_without_any_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No baseline, so the diff gate would pass — the governed policy still fails the job."""
    exit_code = _policy_main(monkeypatch, tmp_path, payload=_policy_payload("fail"))

    assert exit_code == 1
    assert FakePolicyHostedClient.calls == [SERVER_RUN_ID]


def test_main_passes_the_job_on_a_passing_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exit_code = _policy_main(monkeypatch, tmp_path, payload=_policy_payload("pass"))

    assert exit_code == 0
    assert FakePolicyHostedClient.calls == [SERVER_RUN_ID]


def test_main_passes_the_job_on_a_conditional_pass_and_logs_the_caveat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: `conditional_pass` merges, and the job log says why it was not clean."""
    exit_code = _policy_main(
        monkeypatch,
        tmp_path,
        payload=_policy_payload("conditional_pass", reason=CONDITIONAL_PASS_REASON),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "passed, with caveats" in out
    assert "Review before merging." in out


def test_main_does_not_fail_the_job_on_an_all_insufficient_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P3's `budgets: []` + `inconclusive`: undecided, so no exit code is invented from it."""
    reason = "No comparable results: all 14 comparisons scored severity 'insufficient'."
    exit_code = _policy_main(
        monkeypatch,
        tmp_path,
        payload=_policy_payload("inconclusive", reason=reason, budgets=[], blocking_regressions=[]),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert reason in out


def test_main_annotates_but_passes_a_run_that_carried_no_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _policy_main(
        monkeypatch,
        tmp_path,
        payload=_policy_payload("inconclusive", policy_source="none", budgets=[]),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "::warning" in out
    assert "the gate is off" in out


def test_main_fails_a_run_that_carried_no_policy_when_require_policy_is_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exit_code = _policy_main(
        monkeypatch,
        tmp_path,
        payload=_policy_payload("inconclusive", policy_source="none", budgets=[]),
        env={"INPUT_REQUIRE_POLICY": "true"},
    )

    assert exit_code == 1


def test_main_warns_and_falls_back_when_the_policy_check_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _policy_main(
        monkeypatch,
        tmp_path,
        payload={},
        failure=HTTPError("https://api.test/policy-check", 404, "Not Found", {}, None),
    )

    assert exit_code == 0
    assert "404" in capsys.readouterr().err


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


def test_provider_keys_are_redacted_including_deepseek() -> None:
    # Keys reach the CLI through the job env untouched; the log redactor
    # matches on the `_API_KEY` suffix, so a new provider needs no code change.
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "DEEPSEEK_API_KEY": "sk-deepseek-secret",
        "HOME": "/home/runner",
    }
    assert sorted(action._secret_values(env)) == ["sk-ant-secret", "sk-deepseek-secret"]
