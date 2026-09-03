# EvalShift GitHub Action

Run your EvalShift golden suite on every pull request, push the result to hosted
EvalShift, keep one PR comment up to date, and fail the check when your
migration policy says the candidate is not safe to ship.

## For AI coding agents

Point your coding agent at the dense, single-file reference for the piece it is
working on:

- EvalShift CLI: <https://www.evalshift.dev/cli-llms-full.txt>
- EvalShift SDK: <https://www.evalshift.dev/sdk-llms-full.txt>
- EvalShift GitHub Action (CI): <https://www.evalshift.dev/ci-llms-full.txt>
  (source of truth: [llms-full.txt](llms-full.txt) in this repo)

> **Behaviour change — the default gate moved.** `fail-on` now defaults to
> `policy`: hosted EvalShift judges the run against your project's migration
> policy instead of the action counting regressions in the diff. A run that
> regressed but stays inside your policy's budgets now passes, and a run that
> busts a budget now fails even when the aggregate regression count is zero. Set
> `fail-on: regression` to keep the old behaviour. See
> [`fail-on` modes](#fail-on-modes).

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
          fail-on: policy # the default; gates on your migration policy
```

You need two things in repository secrets: `EVALSHIFT_TOKEN` for hosted
EvalShift, and a model provider key for whatever models your suite compares.

## Storing the token

`token` must come from a GitHub **encrypted secret**. There is no other
supported storage:

- Never commit it to a file in the repository, and never paste it into workflow
  YAML as a literal `env:` or `with:` value. Both are readable by anyone who can
  read the repo, and both survive in git history after you delete the line.
- Never expose it to `pull_request_target`. That trigger runs the base
  repository's workflow — with secrets in scope — against a fork's code, so a
  fork PR can exfiltrate the key. Use `pull_request`, which has no secrets on a
  fork run and simply fails on the empty `token`.

A repository secret (Settings → Secrets and variables → Actions → New repository
secret) is enough for most repos, and is what the quick start above uses. For
production repositories prefer an **environment secret**, so the key is scoped
to one environment and can carry required reviewers and branch restrictions:

```yaml
jobs:
  evalshift:
    runs-on: ubuntu-latest
    environment: ci          # the environment that holds EVALSHIFT_TOKEN
    steps:
      - uses: actions/checkout@v7
      - uses: babaliauskas/evalshift-action@v0
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
```

The action registers the token with GitHub's log masking before anything else
runs, and redacts it out of CLI output — but only inside the job. Masking is not
a substitute for storing it correctly.

## Use a scoped service-account key

Mint the token from a **service account**, in the EvalShift web app under
Settings → API tokens → Service accounts (`/app/<org-slug>/settings/tokens`).
A service account is an org-owned machine identity: it outlives whoever set CI
up. A personal token is tied to one person's membership — when they leave, the
membership goes, and your pipeline goes red with it. Do not use a personal token
for CI.

Give the key the least privilege that still works. The action needs exactly two
scopes, and the scope picker on that page speaks the same permission keys:

| Scope        | What needs it |
| ------------ | ------------- |
| `run:create` | `evalshift push` — creating the hosted run and finalizing the upload. |
| `run:read`   | The baseline lookup and the diff this action gates on. |

Set the service account's role to `member`; a `viewer` cannot upload a run.

Two things a correctly-scoped key deliberately cannot do:

- **Auto-create the hosted project.** `project:create` is an owner permission and
  a service account is never an owner. Create the project once in the web app and
  set `create-project: false`, so a wrong project slug fails as a missing project
  rather than looking like a credential problem.
- **Rewrite the project's gating thresholds.** `evalshift push` sends the
  `thresholds:` block from your `evalshift.yaml` whenever one is present, and
  rewriting a project's gating policy needs `policy:configure` — also owner-only.
  Keep thresholds canonical in the web app and out of the config the CI job runs,
  or the push fails with `Project owner role required`.

When the key is missing a permission, the action prints the exact permission key
it was denied and how to fix it before exiting non-zero — you never have to guess
which scope you forgot.

## Rotating the token

1. Rotate the key in the web app. The old one keeps working for a 24-hour grace
   window, so nothing breaks at the moment you click.
2. Update the GitHub secret with the new value.
3. Re-run the workflow, confirm it is green, and let the old key expire.

Doing it in that order means there is never a moment when the running pipeline
has no valid key. Never edit a key's secret in place — rotation exists precisely
so a deploy does not race a credential change.

## Model provider API keys

The action does not manage provider credentials. It passes the job environment
through to the CLI, adding `EVALSHIFT_HOST`, `EVALSHIFT_TOKEN` and
`COLUMNS=512` (the last so rich does not fold the hosted run URL across two
lines) and touching nothing else, so set the key as a job `env:` entry and the
CLI picks it up.

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
| `evalshift-version` | no       | `0.13.1`                    | Exact EvalShift CLI version to install from PyPI. Pin this if you want run-to-run reproducibility across CLI releases. |
| `python-version`    | no       | `3.12`                      | Python version used to install and run the CLI. |
| `fail-on`           | no       | `policy`                    | Gating mode. See below. |
| `branch`            | no       | auto                        | Candidate branch name recorded on the hosted run. Auto-detected from the PR head ref, else the pushed ref. Override only when your branch naming differs from the git ref. |
| `base-branch`       | no       | auto                        | Branch to look for a baseline run on. Auto-detected from the PR base ref, else the current ref. If this resolves to empty, no baseline is fetched and the check always passes. |
| `create-project`    | no       | `true`                      | Whether `evalshift push` may auto-create the hosted project when it does not exist yet. Set `false` to make a missing project a hard failure instead. |
| `comment`           | no       | `true`                      | Whether to create or update the PR comment. Set `false` to keep the commit status but stay out of the conversation. |
| `github-token`      | no       | `github.token`              | Token used for the PR comment and the commit status. Override only when you want the comment posted by a bot account rather than `github-actions`. |
| `repo-private`      | no       | repository visibility       | Whether this repository is private, used for the plan preflight. Defaults to the GitHub context; set it explicitly only if you mirror a private repository into a public one, or vice versa. |

### `fail-on` modes

| Mode                   | Job fails when |
| ---------------------- | -------------- |
| `policy`               | Hosted EvalShift evaluates the run against your project's migration policy and answers `fail`. **This is the default.** |
| `never`                | Never. Records the run and reports, but never blocks the merge. Use while you are still calibrating a suite. |
| `regression`           | The hosted diff reports one or more regressed examples in aggregate. |
| `any-slice-regression` | Any slice's pass rate moved down, even when the aggregate is flat or improved. Stricter — catches a specific slice degrading while overall numbers hide it. |

`policy` is the only mode that gates on what you actually configured. The other
three ask the action's own question about the diff, which can disagree with your
policy in both directions. The decision comes from
`GET /runs/{id}/policy-check`, so the CLI, the web app and this check all
enforce one policy — the action never re-implements a threshold.

The policy answers with one of four statuses. Only one of them fails the job:

| Status | Gate | What it means |
| ------ | ---- | ------------- |
| `pass` | merges | Every budget is within policy. |
| `conditional_pass` | merges | Every budget held and nothing critical or high regressed, but medium/low regressions and/or comparisons that scored zero pairs came with it. A pass with caveats — deliberately **not** a gate failure. The comment says so and prints the server's reason; read it before merging. |
| `fail` | **blocks** | A budget was busted, or a critical/high regression is blocking. |
| `inconclusive` | merges | The policy could not decide. Never rendered as a pass — the comment and the commit status say the gate did not decide. Six different causes produce this — including a project with no policy configured at all, and a run in which no blocking evaluator scored a single record — and only the server's `reason` string tells them apart, so the action prints that reason verbatim rather than paraphrasing it. |

Any status outside those four is something a newer server grew that this pinned
version of the action has never seen. It is handled like `inconclusive`: it does
not fail the job, and it is never rendered as a pass.

If the policy check itself is unreachable, 404s, or has no stored decision for
the run, the action does not quietly go green. It falls back to `regression`
gating for that run and says so in the log, the commit status and the PR
comment.

When no compatible baseline run exists on the base branch, there is nothing to
compare against: the diff-based modes pass, `regression_count` is `0`, and the
PR comment says so explicitly. `policy` still asks the server for a verdict.

## Outputs

| Output             | Value |
| ------------------ | ----- |
| `run_url`          | Hosted run URL for this run. |
| `diff_url`         | Hosted diff URL comparing this run to the baseline. Empty string when no compatible baseline was found. |
| `run_id`           | Hosted EvalShift run id — the server-minted id every `/runs/{id}` API route takes. Not the local `r_…` run directory name. |
| `regression_count` | Number of regressed examples in the hosted diff. `0` when there is no baseline. |
| `conclusion`       | `success` or `failure`, reflecting `fail-on`. A policy that declined to decide is a `success` here — read the PR comment or the commit status for the verdict itself. |

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
2. Asks hosted EvalShift whether this job is covered by the organization's plan, before
   spending any model credits. See [Plan limits](#plan-limits).
3. Runs `evalshift all --yes` against your config and suite, writing run state to
   `.evalshift/runs` in the workspace.
4. Pushes the completed run to hosted EvalShift, creating the project if needed.
5. Asks the hosted API for a compatible baseline run on the base branch,
   fetches the diff, and — under `fail-on: policy` — asks the server to judge
   the run against the project's migration policy.
6. Writes the outputs, upserts a single PR comment (marked so it updates in place
   instead of stacking), and sets the `evalshift/regression` commit status.
7. Exits non-zero when the gate says so — the hosted policy verdict under `fail-on: policy`, the diff otherwise.

## Data sent to hosted EvalShift

The action's push is the CLI's push: it uploads the run bundle and nothing else. The
field-by-field contract of what that bundle contains — and what never leaves the runner
(provider API keys, prompt bodies, system prompts, conversation histories, raw provider
responses) — is documented in
[What gets uploaded](https://www.evalshift.dev/docs/what-gets-uploaded). Suite `inputs`,
`expected` outputs, both models' outputs, and tool-call traces upload verbatim, so redact
sensitive values at capture time before they reach the golden suite. Neither the CLI nor the
action carries any telemetry; besides the push, the only hosted call the action adds is the
[plan preflight](#plan-limits) (project slug + repository visibility).

## Plan limits

Some plan limits are cheaper to discover before the suite runs than halfway through it, so the
action asks first: it reads `project: <org>/<project>` from your config, resolves that project,
and calls `POST /projects/{id}/ci-preflight` with the repository's visibility.

If the answer is a `402`, the job stops before any model credits are spent. You get an
`::error::` annotation, a step summary, and — on a pull request — the usual EvalShift comment
carrying the server's message, the plan you're on, what was blocked, and an upgrade link. The
common cases are the monthly run quota and the parallelism cap; private-repo CI is included
on every plan, the free one included.

Anything else — EvalShift being down, a project that doesn't exist yet, a token that can't list
projects — is treated as an infrastructure problem: the action logs a warning and runs anyway.
A billing check that breaks everyone's CI when the billing service is down is worse than one
that occasionally lets a run through, and the server still enforces limits on the upload.

`repo-private` comes from the GitHub context and is *reported* to EvalShift, not verified by
it. The server records the first `true` permanently, so a project that has ever reported
private stays private.

## Dogfood workflow

`.github/workflows/dogfood.yml` runs this action against the fixture project in
`examples/dogfood/` to verify end-to-end wiring: install, run, hosted push,
baseline diff lookup, and action outputs.

It is manual (`workflow_dispatch`) because each run spends real model credits.
Routine CLI drift is covered for free by the `cli-contract` job in `ci.yml`
(it installs the pinned CLI and runs `scripts/cli_contract.sh`);
this workflow is for verifying the whole path, including the hosted API
contract. It gates with `fail-on: never` so a genuine regression in the fixture
suite does not red this repository. Set the `EVALSHIFT_TOKEN` and a model
provider key as repository secrets to enable it; without them the job warns and
skips.

## Versioning

Pin to `@v0` to track the latest v0.x, or to an exact tag such as `@v0.3.0` for
a fully reproducible workflow.

### How the pin is maintained

The `evalshift-version` default in `action.yml` is the single source of truth for
the pinned CLI. Every other mention (this README, `DOCS.md`, `llms-full.txt`) is
asserted equal by `tests/test_pin_consistency.py`, so a stale literal fails
`uv run pytest`. `scripts/bump_cli_pin.py <new-version>` rewrites every site and
bumps the action's patch version.

`.github/workflows/bump-cli-pin.yml` runs that script and opens the PR
(branch `bump/evalshift-<version>`, title `chore(pin): evalshift <old> → <new>`;
re-running updates the same branch). It stops green when `action.yml` already
pins the target. Otherwise the PR is pre-validated before it opens, so it is
never red on arrival:

1. the target's `requires_python` (from PyPI) is checked against the
   `python-version` default in `action.yml` — a mismatch fails with a clear
   error, because that bump needs a human to raise `python-version` too;
2. the target CLI is installed and `scripts/cli_contract.sh` run against it;
3. `scripts/bump_cli_pin.py` rewrites every site and `uv run pytest` passes on
   the rewritten tree.

Merging the PR ships the new pin to every `@v0` consumer (see
[Releases](#releases)). Triggers:

- `schedule`: daily poll of PyPI for the latest `evalshift` release.
- `workflow_dispatch`: optional `version` input to pin a specific release.
- `repository_dispatch` with type `evalshift-cli-release` and
  `client_payload.version`, for a push from the CLI's release workflow.

Optional secret `BUMP_PR_TOKEN`: a PR opened with the default `GITHUB_TOKEN`
does not trigger `ci.yml`. Store a fine-grained PAT with `contents` and
`pull-requests` write on this repo under that name and the normal CI runs on
the bump PR; without it the workflow falls back to `GITHUB_TOKEN` and the PR
still opens, relying on the pre-validation above.

### Releases

Merging a change that bumps `version` in `pyproject.toml` *is* the release.
`.github/workflows/release.yml` runs on every push to `main`: if tag
`v<version>` does not exist yet it creates it on the merge commit, force-moves
the floating `v0` tag to the same commit (only `v0` is ever force-pushed; exact
tags are never moved), and publishes a GitHub Release with notes since the
previous `v0.*` tag. Ordinary merges, where the tag already exists, stop green.
So a merged bump PR ships the new default pin to every `@v0` consumer
immediately. The workflow refuses a `pyproject.toml` major other than `0`:
moving `v0` onto a 1.x commit would be wrong, and that day needs a `v1` tag and
a change to this section.

To cut a release by hand: bump `version` in `pyproject.toml` in a PR, merge it,
done. Never push tags manually — `release.yml` owns `v<version>` and `v0`.

## License

MIT — see [LICENSE](LICENSE). The EvalShift CLI this action installs is licensed
separately.
