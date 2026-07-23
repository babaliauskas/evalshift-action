# EvalShift GitHub Action Documentation

The EvalShift GitHub Action turns your golden suite into a **merge gate**. On every pull
request it runs the suite against both models, pushes the result to hosted EvalShift, compares
it to the latest run on your base branch, and fails the check when the diff shows a regression.

- **Action ref:** `babaliauskas/evalshift-action@v0` · **version:** 0.1.0 · **License:** MIT
- **Kind:** composite action — installs Python + the pinned EvalShift CLI, then runs a small
  stdlib-only helper script. Nothing is compiled, nothing is containerised.
- **Pinned CLI:** `evalshift==0.9.0` by default, overridable.
- **What it adds on top of the CLI:** hosted push, baseline lookup, cross-branch diff, one
  self-updating PR comment, a commit status, and an exit code.

---

## Table of contents

1. [Who this is for](#who-this-is-for)
2. [Prerequisites](#prerequisites)
3. [Quick start](#quick-start)
4. [Secrets and provider keys](#secrets-and-provider-keys)
5. [Inputs](#inputs)
6. [Outputs](#outputs)
7. [Gating: the `fail-on` modes](#gating-the-fail-on-modes)
8. [What lands on the pull request](#what-lands-on-the-pull-request)
9. [Permissions](#permissions)
10. [How it works, step by step](#how-it-works-step-by-step)
11. [Branch and baseline resolution](#branch-and-baseline-resolution)
12. [Cost control](#cost-control)
13. [Recipes](#recipes)
14. [Security model](#security-model)
15. [Limits and known edges](#limits-and-known-edges)
16. [Troubleshooting](#troubleshooting)
17. [Versioning and stability](#versioning-and-stability)
18. [FAQ](#faq)

---

## Who this is for

You already have an EvalShift golden suite and you run it by hand before shipping a model or
prompt change. That works right up until it doesn't: someone edits a system prompt on a Friday,
nobody re-runs the suite, and the regression ships.

This action closes that gap. It makes "did this change make the model worse?" a required check,
answered by the same statistics you'd get locally, on a pull request, before anyone can merge.

If you don't have a suite yet, start with the CLI — `pip install evalshift && evalshift demo`
gives you a working project in one command. Come back here once `evalshift all` passes locally.

---

## Prerequisites

Four things, all required:

| # | Requirement | How to get it |
| - | ----------- | ------------- |
| 1 | `evalshift.yaml` committed to the repo | `evalshift init` (or `evalshift demo` for a scaffolded example) |
| 2 | A golden JSONL suite committed | `evalshift init` writes one; `evalshift capture sync` grows it from production captures |
| 3 | Repository secret `EVALSHIFT_TOKEN` | Hosted EvalShift → org settings → tokens. Starts with `es_`. |
| 4 | A model provider API key as a repository secret | Whichever provider your config's models belong to |

Verify locally first. If `evalshift all --yes` doesn't pass on your machine, it will not pass on
a runner — you'll just pay for the model calls to find out.

---

## Quick start

Create `.github/workflows/evalshift.yml`:

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

Keep the `push: branches: [main]` trigger. Pull requests need something to compare against, and
that something is the most recent run on your base branch. Without trunk runs, every PR reports
"no baseline" and passes unconditionally.

The CLI's `evalshift init --ci` scaffolds a near-identical workflow for you.

### What you'll see on the first PR

The check goes green and the comment says no compatible baseline was found. That's correct
behavior, not a misconfiguration — there's no trunk run yet to diff against. Merge it, let the
`push` trigger record a baseline on `main`, and the next PR gets a real comparison.

---

## Secrets and provider keys

The action does **not** manage provider credentials. It passes the job environment through to
the CLI unchanged, so set the key as a job-level `env:` entry and the CLI picks it up.

| Provider  | Environment variable                 |
| --------- | ------------------------------------ |
| Anthropic | `ANTHROPIC_API_KEY`                  |
| OpenAI    | `OPENAI_API_KEY`                     |
| Google    | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

Which key you need follows from `defaults.source_model` and `defaults.target_model` in your
`evalshift.yaml`. Comparing across two providers means both keys:

```yaml
    env:
      EVALSHIFT_NONINTERACTIVE: "1"
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`EVALSHIFT_NONINTERACTIVE: "1"` is recommended. The action already passes `--yes`, which skips
the CLI's cost confirmation, but the env var covers any other prompt — and a prompt on a runner
means a hung job.

---

## Inputs

| Input | Required | Default | What it does |
| ----- | -------- | ------- | ------------ |
| `token` | yes | — | Hosted EvalShift API token, an `es_...` value. Masked in logs and redacted from CLI output. |
| `host` | no | `https://api.evalshift.dev` | Hosted API base URL. Set only for a self-hosted or staging deployment. |
| `config` | no | `evalshift.yaml` | Path to your config, relative to the repository root. Paths *inside* the config (prompt files, tools) resolve relative to the config file's own directory, so a config in a subdirectory works. |
| `suite` | no | `golden.jsonl` | Path to the golden JSONL suite, relative to the repository root. |
| `evalshift-version` | no | `0.9.0` | Exact CLI version installed from PyPI. Pin this for run-to-run reproducibility across CLI releases. |
| `python-version` | no | `3.14` | Python used to install and run the CLI. Must satisfy the CLI's minimum (3.14 for 0.9.0). |
| `fail-on` | no | `regression` | Gating mode. See [below](#gating-the-fail-on-modes). |
| `branch` | no | auto | Candidate branch name recorded on the hosted run. Auto-detected from the PR head ref, else the pushed ref. |
| `base-branch` | no | auto | Branch to look for a baseline run on. Auto-detected from the PR base ref, else the current ref. Resolving to empty means no baseline is fetched and the check always passes. |
| `create-project` | no | `true` | Whether `evalshift push` may auto-create the hosted project when it doesn't exist. Set `false` to make a missing project a hard failure. |
| `comment` | no | `true` | Whether to create or update the PR comment. Set `false` to keep the commit status but stay out of the conversation. |
| `github-token` | no | `github.token` | Token used for the PR comment and the commit status. Override only to have a bot account post instead of `github-actions`. |

Boolean inputs accept `1`, `true`, `yes`, `on` (case-insensitive). Anything else is false.

---

## Outputs

| Output | Value |
| ------ | ----- |
| `run_url` | Hosted run URL for this run. |
| `diff_url` | Hosted diff URL comparing this run to the baseline. Empty string when no compatible baseline was found. |
| `run_id` | EvalShift run id, usable with `evalshift` CLI commands locally. |
| `regression_count` | Number of regressed examples in the hosted diff. `0` when there is no baseline. |
| `conclusion` | `success` or `failure`, reflecting `fail-on`. |

Consume them from a later step:

```yaml
      - uses: babaliauskas/evalshift-action@v0
        id: evalshift
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
      - run: echo "Hosted diff ${{ steps.evalshift.outputs.diff_url }}"
```

Outputs are written before the comment and status calls, so they're still available even if the
job lacks permission to comment.

---

## Gating: the `fail-on` modes

| Mode | The job fails when |
| ---- | ------------------ |
| `never` | Never. Records the run, pushes it, comments — but never blocks the merge. Use while you're still calibrating a suite. |
| `regression` | The hosted diff reports one or more regressed examples in aggregate. **Default.** |
| `any-slice-regression` | Any slice's pass rate moved down, even when the aggregate is flat or improved. Stricter — catches one slice degrading while the overall number hides it. |

`any-slice-regression` is not simply "stricter than `regression`" in every case; it's a
different question. A run where the aggregate regression count is above zero but no individual
slice moved down will fail under `regression` and pass under `any-slice-regression`. If you
want both guarantees, run the action twice with different modes (and `comment: "false"` on one
of them), or keep `regression` and rely on slices for diagnosis rather than gating.

**Suggested progression:** start at `never` for a week or two while the suite settles, move to
`regression` once the signal is trustworthy, and adopt `any-slice-regression` only when you have
slices you genuinely care about individually — a safety slice, a high-value customer segment.

When no compatible baseline run exists on the base branch, there's nothing to compare against:
the check passes, `regression_count` is `0`, and the PR comment says so explicitly.

---

## What lands on the pull request

### One comment, updated in place

The action maintains exactly one comment per PR, marked with a hidden HTML marker so it edits
itself on every push instead of stacking up. With a baseline present it looks like this:

> ## EvalShift regression check
>
> **Conclusion:** `failure`
> **Hosted run:** [open run](https://app.evalshift.dev/…)
> **Regressions:** 3
> **Diff:** [compare to baseline](https://app.evalshift.dev/…)
> **Pass-rate movement:** -12 pts
>
> | Slice | Pass-rate delta |
> | --- | ---: |
> | safety_refusals | -25 pts |
> | tool_selection | -8 pts |

Up to five regressed slices are listed, worst first. When nothing regressed you get a single
`No regressed slices` row. Percentages are rounded for display only — gating uses the raw
values, so a slice can appear as `0 pts` and still count as a regression.

Without a baseline, the table is replaced by a single line: *No compatible baseline run was
found on the base branch.*

### A commit status

Context `evalshift/regression`, linking to the hosted diff (or the run, when there's no diff).
This is what you add to branch protection to make EvalShift a required check. It's set on push
events too, not just pull requests.

---

## Permissions

| Permission | Why |
| ---------- | --- |
| `contents: read` | Checking out the repository. |
| `pull-requests: write` | Posting the PR comment. |
| `issues: write` | PR comments are issue comments in the GitHub API. |
| `statuses: write` | Setting the `evalshift/regression` commit status. |

Only `contents: read` is strictly required. If the comment or status permissions are missing,
the action logs a warning and carries on rather than failing the run — the gate still works.

---

## How it works, step by step

1. **Install.** `actions/setup-python` at `python-version`, then `pip install
   evalshift==<evalshift-version>`. No pip caching, so budget roughly 20–60 seconds.
2. **Run.** `evalshift all --yes --config <config> --suite <suite>` in the workspace root.
   This is the full local pipeline: doctor → run → evaluate → analyze → report. Artifacts land
   in `.evalshift/runs/<run-id>/`, including the self-contained `report.html`.
3. **Push.** `evalshift push <run-id>` uploads the run bundle to hosted EvalShift, creating the
   project if `create-project` allows it. Git metadata from the runner environment travels with
   the bundle so the server can pair this run with base-branch runs later.
4. **Find a baseline.** Asks the hosted API for the latest compatible run on the base branch.
   "Compatible" is a server-side judgement — a suite that changed shape can't be diffed against
   an older one.
5. **Fetch the diff.** Pulls aggregate and per-slice deltas from the hosted API.
6. **Report.** Writes the five outputs, upserts the PR comment, sets the commit status.
7. **Gate.** Exits non-zero when `fail-on` says the diff is a regression.

The action is a wrapper, not a reimplementation. All evaluation and statistics happen in the
CLI; all cross-branch diffing happens server-side. If you want to understand what
`regression_count` actually means, read the CLI's statistical methodology docs — it's paired
statistics with Benjamini-Hochberg correction, not a threshold on an average.

### One thing that surprises people

Output is captured per command and printed when that command finishes, not streamed live. A
suite that takes six minutes looks like a hung job for six minutes. It isn't.

---

## Branch and baseline resolution

The action figures out two branch names, and both matter:

- **`branch`** — the candidate, recorded on the hosted run. From the PR head ref, else the
  pushed ref.
- **`base-branch`** — where to look for a baseline. From the PR base ref, else the current ref.

On a `push` event, `base-branch` falls back to the branch being pushed. A push to `main`
therefore diffs against the *previous* `main` run. That's intentional: it tracks trunk drift
over time, and it's how baselines get recorded in the first place.

Override `branch` / `base-branch` only when your branch naming genuinely differs from your git
refs — for example if you push through a mirror that rewrites ref names. If `base-branch`
resolves to an empty string, the action skips the baseline lookup entirely and always passes.

---

## Cost control

Every run makes real model calls against both the source and target model. Suite size × 2 is
your per-run cost, and the runner starts with a cold cache every time, so nothing is free on
repeat runs the way it is locally.

Practical levers, in order of effect:

**Only run when it matters.** Most PRs don't touch prompts or the suite:

```yaml
on:
  pull_request:
    paths:
      - "eval/**"
      - "app/prompts/**"
      - "evalshift.yaml"
  push:
    branches: [main]
```

**Keep the CI suite smaller than your full local suite.** A 40-example CI suite that runs on
every PR catches more regressions in practice than a 500-example suite you disable after the
first invoice.

**Use cheap evaluators in CI.** Structural evaluators cost nothing. LLM-judge evaluators are a
third model call per example. If your local config leans on judges, consider a CI-specific
config pointed at with `config:`.

**Don't gate on draft PRs:**

```yaml
    if: github.event.pull_request.draft == false
```

---

## Recipes

### Config in a subdirectory

```yaml
      - uses: babaliauskas/evalshift-action@v0
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          config: eval/evalshift.yaml
          suite: eval/golden.jsonl
```

Paths inside the config resolve relative to the config file, so `prompts.py` next to
`eval/evalshift.yaml` needs no path changes.

### Report-only while calibrating

```yaml
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          fail-on: never
```

### Two suites in one repository

Give each suite its own job, and let only **one** of them own the PR comment:

```yaml
jobs:
  eval-text:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: babaliauskas/evalshift-action@v0
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          suite: eval/golden-text.jsonl

  eval-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: babaliauskas/evalshift-action@v0
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          config: eval/agent.yaml
          suite: eval/golden-agent.jsonl
          comment: "false"
```

The comment marker and the commit status context are both constants, so two commenting
invocations on the same PR overwrite each other's output. One owner, always.

### Keep the HTML report as a build artifact

The action doesn't upload artifacts. Add a step if you want the report retained on the run:

```yaml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: evalshift-report
          path: .evalshift/runs/**/report.html
```

### Make it a required check

Branch protection → require status checks → add `evalshift/regression`. Do this only after the
suite has been running at `fail-on: never` long enough that you trust it.

### Post as a bot account

```yaml
        with:
          token: ${{ secrets.EVALSHIFT_TOKEN }}
          github-token: ${{ secrets.MY_BOT_PAT }}
```

Note the comment upsert only edits comments authored by a **Bot** account. A PAT belonging to a
human user will create a new comment on every run instead of updating one.

---

## Security model

- **Secrets are masked and redacted.** The hosted token and GitHub token are registered with
  GitHub's log masking before anything else runs. On top of that, the action redacts any
  environment value whose name contains `TOKEN` or `SECRET`, or ends in `API_KEY`, out of the
  CLI's stdout and stderr before printing it — so a CLI that echoes a key in an error message
  doesn't leak it into your logs.
- **Tokens never appear in argv.** The hosted token and host URL reach the CLI through the
  environment only, so they can't show up in a process listing or a `command failed:` message.
- **Provider keys never leave the job.** The action passes them to the CLI and nowhere else.
  They are not uploaded to hosted EvalShift, not written to the bundle, not sent to GitHub.
- **The action never writes to your repository.** No commits, no pushes, no file mutations
  outside `.evalshift/` in the workspace.
- **Fork PRs.** Secrets aren't available to workflows triggered by `pull_request` from a fork,
  so the action will fail on `token` being empty. That's GitHub's design, and working around it
  with `pull_request_target` means running untrusted code with your secrets in scope — don't,
  unless you fully understand the exposure.
- **Dependencies.** The runtime helper is stdlib-only. `pip-audit` runs in this repo's own CI.

---

## Limits and known edges

Worth knowing before you rely on this in anger:

- **The PR comment lookup reads only the first page of comments.** On a very long PR thread the
  EvalShift comment can fall off page one, and a second comment gets created instead of the
  first being updated.
- **The comment marker and status context are global constants.** Parallel invocations on the
  same PR overwrite each other. One commenting invocation per PR.
- **The run id is the newest directory under `.evalshift/runs`.** If a step between the run and
  the push touches an older run directory's mtime, the wrong run gets pushed. In a normal
  workflow this never happens.
- **The hosted run URL is parsed from CLI stdout.** A CLI release that changes how the push
  result is printed would break this. The repo's `cli-contract` CI job guards flag renames but
  not output shape.
- **No retries on hosted API calls.** A 30-second timeout, one attempt. A transient hosted
  outage fails the step rather than silently passing — deliberate, but it means a flaky network
  reads as a failed job.
- **`fail-on` decides the exit code, not whether the run happened.** Even at `never`, the run
  executes, costs money, and pushes.

---

## Troubleshooting

### `input 'token' is required`

The `token:` input is empty. Either the secret isn't set, or this is a fork PR where secrets
aren't exposed.

### `command failed (1): evalshift all --yes ...`

The CLI itself failed — bad config, missing provider key, model API error. The CLI's own
(redacted) stderr is printed directly above this line. Reproduce with the same command locally.

### `no local EvalShift runs found in .../.evalshift/runs`

`evalshift all` exited successfully but wrote nothing where the action looks. Usually a config
that redirects run artifacts elsewhere, or a working-directory mismatch.

### `evalshift push did not print a hosted run URL`

The push didn't emit a URL on its last output line. Run `evalshift push <run-id>` locally
against the same host and see what it prints. Also check for a CLI version mismatch.

### HTTP 401 or 403 from the hosted API

Bad or expired `EVALSHIFT_TOKEN`, wrong `host`, or a project-scoped token trying to auto-create
a project. Verify with `evalshift whoami` locally using the same token.

### `warning: could not upsert PR comment: HTTP 403`

Missing `pull-requests: write` / `issues: write`, or a fork PR with a read-only token. The
gating still works — only the comment is lost.

### The check is always green

In order of likelihood: no baseline run exists on the base branch yet (add the `push` trigger to
`main` and merge once), `fail-on` is `never`, or `base-branch` resolved to an empty string.

### Two EvalShift comments on one PR

Either two action invocations are commenting, or the original comment fell off the first page of
the comments API on a long thread.

### The job hangs with no output

It doesn't — output is buffered per command and printed when each finishes. A slow suite is
silent while it runs.

### `pip install evalshift==0.9.0` fails

`python-version` is below the CLI's minimum. EvalShift 0.9.0 needs Python 3.14+.

### Costs are higher than expected

The runner cache is cold every run. Narrow the trigger with `on.pull_request.paths`, shrink the
CI suite, or swap LLM-judge evaluators for structural ones in a CI-specific config.

---

## Versioning and stability

Pin to `@v0` to track the latest v0.x, or to an exact tag such as `@v0.1.0` for a fully
reproducible workflow. The `evalshift-version` input pins the CLI separately — pin both if you
want a workflow that behaves identically six months from now.

This repo's CI includes a `cli-contract` job that installs the exact pinned CLI version and
asserts the command-line surface the action depends on still exists. It costs nothing (no API
keys, no model credits) and it's the early-warning system for CLI drift. A separate manual
`dogfood` workflow exercises the whole path — install, run, hosted push, baseline lookup,
outputs — against a four-example fixture project; it's manual because it spends real credits.

The action is MIT licensed. The EvalShift CLI it installs is licensed separately
(AGPL-3.0-or-later).

---

## FAQ

**Does this replace the CLI?**
No. It runs the CLI. Everything you can inspect locally — `report.html`, `analysis.json`, the
raw model outputs — is still produced, in the runner's workspace under `.evalshift/runs/`.

**Can I use it without hosted EvalShift?**
Not currently. The baseline lookup and the diff are server-side; without a hosted token there's
nothing to compare against. If you want local-only CI gating, use `evalshift all` directly plus
a migration policy in your config, and skip this action.

**Does it upload my model outputs?**
It pushes the run bundle — manifest, examples, outputs, scores, analysis, and the HTML report —
to hosted EvalShift. It never uploads provider API keys. If your suite contains sensitive
production data, that's the thing to weigh.

**Why did the check pass when the report clearly shows a regression?**
Two common reasons. `fail-on: never` is set, or there was no compatible baseline so nothing was
compared. The comment states which.

**Can I run it on a schedule instead of on PRs?**
Yes — it works on any trigger. On non-PR events you get the commit status and the outputs but no
comment. A nightly run against `main` is a reasonable way to catch provider-side model drift.

**Does it work on self-hosted runners?**
Yes, provided the runner can install Python and reach PyPI, your model provider, and the hosted
API.

**How long does a run take?**
Install is 20–60 seconds. After that it's however long your suite takes at your configured
concurrency, times two models. A 40-example suite is typically a few minutes.

---

## Further reading

- EvalShift CLI documentation — the pipeline, evaluators, statistics, and config schema
- EvalShift SDK documentation — capturing production runs into golden suites
- `examples/dogfood/` in this repo — a complete four-example fixture project
- `llms-full.txt` in this repo — the same material, compressed for AI tools
