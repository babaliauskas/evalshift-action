# EvalShift GitHub Action

Run your EvalShift golden suite on every pull request, push the result to hosted
EvalShift, keep one PR comment up to date, and fail the check when the hosted
diff shows a regression against the base branch.

## Quick start

```yaml
name: evalshift

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  pull-requests: write
  issues: write
  statuses: write

jobs:
  evalshift:
    runs-on: ubuntu-latest
    env:
      EVALSHIFT_NONINTERACTIVE: "1"
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v7
      - uses: babaliauskas/evalshift-action@v0
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          fail-on: regression
```

You need two things in repository secrets: `EVALSHIFT_TOKEN` for hosted
EvalShift, and a model provider key for whatever models your suite compares.

## Model provider API keys

The action does not manage provider credentials. It passes the job environment
through to the CLI unchanged, so set the key as a job `env:` entry and the CLI
picks it up.

| Provider  | Environment variable                  |
| --------- | ------------------------------------- |
| Anthropic | `ANTHROPIC_API_KEY`                   |
| OpenAI    | `OPENAI_API_KEY`                      |
| Google    | `GEMINI_API_KEY` or `GOOGLE_API_KEY`  |

Which key you need follows from `defaults.source_model` and
`defaults.target_model` in your `evalshift.yaml`. Comparing models across two
providers requires both keys:

```yaml
    env:
      EVALSHIFT_NONINTERACTIVE: "1"
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Every run makes real model calls and spends real credits. Suite size times two
models is your per-run cost.

`EVALSHIFT_NONINTERACTIVE: "1"` is recommended: it suppresses the CLI's cost
confirmation prompt, which has no answer on a runner.

## Inputs

| Input               | Required | Default                     | What it does |
| ------------------- | -------- | --------------------------- | ------------ |
| `token`             | yes      | —                           | Hosted EvalShift API token, an `es_...` value. Masked in logs and redacted from CLI output. |
| `host`              | no       | `https://api.evalshift.dev` | Hosted API base URL. Set this only for a self-hosted or staging deployment. |
| `config`            | no       | `evalshift.yaml`            | Path to your EvalShift config, relative to the repository root. Paths *inside* the config (prompt files, tools) resolve relative to the config file's own directory, so a config in a subdirectory works. |
| `suite`             | no       | `golden.jsonl`              | Path to the golden JSONL suite, relative to the repository root. |
| `evalshift-version` | no       | `0.9.0`                     | Exact EvalShift CLI version to install from PyPI. Pin this if you want run-to-run reproducibility across CLI releases. |
| `python-version`    | no       | `3.14`                      | Python version used to install and run the CLI. |
| `fail-on`           | no       | `regression`                | Gating mode. See below. |
| `branch`            | no       | auto                        | Candidate branch name recorded on the hosted run. Auto-detected from the PR head ref, else the pushed ref. Override only when your branch naming differs from the git ref. |
| `base-branch`       | no       | auto                        | Branch to look for a baseline run on. Auto-detected from the PR base ref, else the current ref. If this resolves to empty, no baseline is fetched and the check always passes. |
| `create-project`    | no       | `true`                      | Whether `evalshift push` may auto-create the hosted project when it does not exist yet. Set `false` to make a missing project a hard failure instead. |
| `comment`           | no       | `true`                      | Whether to create or update the PR comment. Set `false` to keep the commit status but stay out of the conversation. |
| `github-token`      | no       | `github.token`              | Token used for the PR comment and the commit status. Override only when you want the comment posted by a bot account rather than `github-actions`. |

### `fail-on` modes

| Mode                   | Job fails when |
| ---------------------- | -------------- |
| `never`                | Never. Records the run and reports, but never blocks the merge. Use while you are still calibrating a suite. |
| `regression`           | The hosted diff reports one or more regressed examples in aggregate. This is the default. |
| `any-slice-regression` | Any slice's pass rate moved down, even when the aggregate is flat or improved. Stricter — catches a specific slice degrading while overall numbers hide it. |

When no compatible baseline run exists on the base branch, there is nothing to
compare against: the check passes, `regression_count` is `0`, and the PR comment
says so explicitly.

## Outputs

| Output             | Value |
| ------------------ | ----- |
| `run_url`          | Hosted run URL for this run. |
| `diff_url`         | Hosted diff URL comparing this run to the baseline. Empty string when no compatible baseline was found. |
| `run_id`           | EvalShift run id, usable with `evalshift` CLI commands locally. |
| `regression_count` | Number of regressed examples in the hosted diff. `0` when there is no baseline. |
| `conclusion`       | `success` or `failure`, reflecting `fail-on`. |

Consume them from a later step:

```yaml
      - uses: babaliauskas/evalshift-action@v0
        id: evalshift
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
      - run: echo "Hosted diff ${{ steps.evalshift.outputs.diff_url }}"
```

## Permissions

| Permission             | Why |
| ---------------------- | --- |
| `contents: read`       | Checking out the repository. |
| `pull-requests: write` | Posting the PR comment. |
| `issues: write`        | PR comments are issue comments in the GitHub API. |
| `statuses: write`      | Setting the `evalshift/regression` commit status. |

Only `contents: read` is strictly required. If the comment or status permissions
are missing, the action logs a warning and carries on rather than failing the
run — the gate still works.

## What the action does

1. Installs Python and the pinned EvalShift CLI.
2. Runs `evalshift all --yes` against your config and suite, writing run state to
   `.evalshift/runs` in the workspace.
3. Pushes the completed run to hosted EvalShift, creating the project if needed.
4. Asks the hosted API for a compatible baseline run on the base branch and
   fetches the diff.
5. Writes the outputs, upserts a single PR comment (marked so it updates in place
   instead of stacking), and sets the `evalshift/regression` commit status.
6. Exits non-zero when `fail-on` says the diff is a regression.

## Dogfood workflow

`.github/workflows/dogfood.yml` runs this action against the fixture project in
`examples/dogfood/` to verify end-to-end wiring: install, run, hosted push,
baseline diff lookup, and action outputs.

It is manual (`workflow_dispatch`) because each run spends real model credits.
Routine CLI drift is covered for free by the `cli-contract` job in `ci.yml`;
this workflow is for verifying the whole path, including the hosted API
contract. It gates with `fail-on: never` so a genuine regression in the fixture
suite does not red this repository. Set the `EVALSHIFT_TOKEN` and a model
provider key as repository secrets to enable it; without them the job warns and
skips.

## Versioning

Pin to `@v0` to track the latest v0.x, or to an exact tag such as `@v0.1.0` for
a fully reproducible workflow.

## License

MIT — see [LICENSE](LICENSE). The EvalShift CLI this action installs is licensed
separately.
