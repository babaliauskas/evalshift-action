from __future__ import annotations

import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError

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

    assert config.evalshift_version == "0.11.0"


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
    config = action.ActionConfig.from_env({"INPUT_TOKEN": "es_secret"})

    assert config.fail_on == "policy"


def test_manifest_fail_on_default_matches_the_script_default() -> None:
    assert _manifest_input_default("fail-on") == "policy"


def test_action_config_rejects_an_unknown_fail_on_mode() -> None:
    with pytest.raises(action.ActionError) as excinfo:
        action.ActionConfig.from_env({"INPUT_TOKEN": "es_secret", "INPUT_FAIL_ON": "sometimes"})

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
        policy=_policy_payload(
            "inconclusive", reason=reason, budgets=[], blocking_regressions=[]
        ),
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

    client = action.HostedClient(
        "https://api.evalshift.test", "es_secret", request=failing_request
    )

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


def _http_error(code: int, body: bytes | None = None) -> HTTPError:
    return HTTPError(
        "https://api.evalshift.test/projects/p-1/ci-preflight",
        code,
        "Payment Required",
        {},
        io.BytesIO(body) if body is not None else None,
    )


def test_ci_preflight_posts_the_visibility_flag_and_parallelism() -> None:
    requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None = None,
    ) -> dict[str, Any]:
        requests.append((method, url, headers, data))
        return {"allowed": True}

    client = action.HostedClient("https://api.evalshift.test", "es_secret", request=fake_request)

    client.ci_preflight("p-1", repo_private=True)

    method, url, headers, data = requests[0]
    assert method == "POST"
    assert url == "https://api.evalshift.test/projects/p-1/ci-preflight"
    assert headers["Authorization"] == "Bearer es_secret"
    assert data is not None
    assert json.loads(data) == {
        "repo_private": True,
        "parallelism": action.PREFLIGHT_PARALLELISM,
    }


def test_ci_preflight_402_carries_the_servers_message_and_details() -> None:
    def denied_request(*args: Any, **kwargs: Any) -> Any:
        raise _http_error(402, DENIAL_BODY)

    client = action.HostedClient("https://api.evalshift.test", "es_secret", request=denied_request)

    with pytest.raises(action.PreflightDenied) as excinfo:
        client.ci_preflight("p-1", repo_private=True)

    denial = excinfo.value
    assert denial.message == "Private-repo CI is not included in the Free plan."
    assert denial.details["feature"] == "private_repo_ci"
    assert denial.details["tier"] == "free"


def test_find_project_id_matches_the_project_slug() -> None:
    def fake_request(*args: Any, **kwargs: Any) -> Any:
        return [
            {"id": "p-0", "slug": "other"},
            {"id": "p-1", "slug": "checkout"},
        ]

    client = action.HostedClient("https://api.evalshift.test", "es_secret", request=fake_request)

    assert client.find_project_id("acme", "checkout") == "p-1"
    assert client.find_project_id("acme", "missing") is None


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


def test_run_preflight_returns_the_denial_on_402() -> None:
    class DeniedClient:
        def find_project_id(self, org_slug: str, project_slug: str) -> str:
            return "p-1"

        def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
            raise action.PreflightDenied("nope", {"feature": "private_repo_ci"})

    denial = action.run_preflight(
        DeniedClient(), project_ref=("acme", "checkout"), repo_private=True
    )

    assert denial is not None
    assert denial.message == "nope"


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(500),
        _http_error(404),
        _http_error(403),
        URLError("connection refused"),
    ],
)
def test_run_preflight_never_blocks_on_an_infrastructure_failure(
    failure: Exception,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail-open on infrastructure: an EvalShift outage must not break every customer's CI."""

    class BrokenClient:
        def find_project_id(self, org_slug: str, project_slug: str) -> str:
            return "p-1"

        def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
            raise failure

    assert (
        action.run_preflight(BrokenClient(), project_ref=("acme", "checkout"), repo_private=True)
        is None
    )
    assert "preflight" in capsys.readouterr().err


def test_run_preflight_is_skipped_when_the_project_is_not_hosted_yet() -> None:
    class EmptyClient:
        def find_project_id(self, org_slug: str, project_slug: str) -> None:
            return None

        def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
            raise AssertionError("preflight must not run without a resolved project")

    assert (
        action.run_preflight(EmptyClient(), project_ref=("acme", "checkout"), repo_private=True)
        is None
    )


def test_run_preflight_is_skipped_without_a_project_ref() -> None:
    class UnusedClient:
        def find_project_id(self, org_slug: str, project_slug: str) -> str:
            raise AssertionError("no project ref means nothing to look up")

    assert action.run_preflight(UnusedClient(), project_ref=None, repo_private=True) is None


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


class FakePreflightHostedClient:
    """Stands in for ``HostedClient`` in ``main`` — denies the preflight, records nothing else."""

    denied: ClassVar[bool] = True
    calls: ClassVar[list[tuple[str, bool]]] = []

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.token = token

    def find_project_id(self, org_slug: str, project_slug: str) -> str:
        return "p-1"

    def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
        type(self).calls.append((project_id, repo_private))
        if type(self).denied:
            raise action.PreflightDenied(
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

    def baseline_compatible(self, run_id: str, branch: str) -> dict[str, Any]:
        return {}

    def run_diff(self, api_diff_url: str) -> dict[str, Any]:
        return {}

    def policy_check(self, run_id: str) -> dict[str, Any]:
        return _policy_payload("pass")


def _preflight_workspace(tmp_path: Path) -> None:
    (tmp_path / "evalshift.yaml").write_text("version: 1\nproject: acme/checkout\n", "utf-8")


def test_a_denied_preflight_fails_the_job_without_running_the_suite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _preflight_workspace(tmp_path)
    summary = tmp_path / "summary.md"
    FakePreflightHostedClient.denied = True
    FakePreflightHostedClient.calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(action, "HostedClient", FakePreflightHostedClient)

    def refuse_to_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the suite must not run after a denied preflight")

    monkeypatch.setattr(action, "run_evalshift_commands", refuse_to_run)
    for key in list(os.environ):
        if key.startswith(("INPUT_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_TOKEN", "es_secret")
    monkeypatch.setenv("INPUT_REPO_PRIVATE", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    exit_code = action.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert FakePreflightHostedClient.calls == [("p-1", True)]
    assert "::error title=EvalShift::" in captured.out
    assert "Private-repo CI is not included in the Free plan." in captured.out
    assert "https://app.evalshift.dev/app/acme/settings/billing" in summary.read_text("utf-8")


def test_an_allowed_preflight_lets_the_suite_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preflight_workspace(tmp_path)
    FakePreflightHostedClient.denied = False
    FakePreflightHostedClient.calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(action, "HostedClient", FakePreflightHostedClient)
    ran: list[bool] = []

    def fake_run(*args: Any, **kwargs: Any) -> action.EvalShiftRunResult:
        ran.append(True)
        return action.EvalShiftRunResult(run_id="run-1", run_url="https://app.test/run")

    monkeypatch.setattr(action, "run_evalshift_commands", fake_run)
    for key in list(os.environ):
        if key.startswith(("INPUT_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_TOKEN", "es_secret")
    monkeypatch.setenv("INPUT_REPO_PRIVATE", "false")

    exit_code = action.main()

    assert exit_code == 0
    assert ran == [True]
    assert FakePreflightHostedClient.calls == [("p-1", False)]


def test_repo_private_input_defaults_to_the_github_context() -> None:
    assert _manifest_input_default("repo-private") == "${{ github.event.repository.private }}"


class FakePolicyHostedClient:
    """Stands in for ``HostedClient`` in ``main``: no preflight, one policy decision."""

    payload: ClassVar[dict[str, Any]] = {}
    failure: ClassVar[Exception | None] = None
    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.token = token

    def find_project_id(self, org_slug: str, project_slug: str) -> str | None:
        return None

    def ci_preflight(self, project_id: str, *, repo_private: bool) -> None:
        raise AssertionError("an unhosted project has nothing to preflight")

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
        lambda *args, **kwargs: action.EvalShiftRunResult(
            run_id="run-1", run_url="https://app.test/run"
        ),
    )
    for key in list(os.environ):
        if key.startswith(("INPUT_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_TOKEN", "es_secret")
    return action.main()


def test_main_fails_the_job_on_a_failing_policy_without_any_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No baseline, so the diff gate would pass — the governed policy still fails the job."""
    exit_code = _policy_main(monkeypatch, tmp_path, payload=_policy_payload("fail"))

    assert exit_code == 1
    assert FakePolicyHostedClient.calls == ["run-1"]


def test_main_passes_the_job_on_a_passing_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exit_code = _policy_main(monkeypatch, tmp_path, payload=_policy_payload("pass"))

    assert exit_code == 0
    assert FakePolicyHostedClient.calls == ["run-1"]


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
        payload=_policy_payload(
            "inconclusive", reason=reason, budgets=[], blocking_regressions=[]
        ),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert reason in out


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
