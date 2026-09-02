# EvalShift GitHub Action Documentation

The EvalShift GitHub Action turns your golden suite into a **merge gate**. On every pull
request it runs the suite against both models, pushes the result to hosted EvalShift, compares
it to the latest run on your base branch, and fails the check when your migration policy says
the candidate is not safe to ship.

- **Action ref:** `babaliauskas/evalshift-action@v0` · **version:** 0.3.0 · **License:** MIT
- **Kind:** composite action — installs Python + the pinned EvalShift CLI, then runs a small
  stdlib-only helper script. Nothing is compiled, nothing is containerised.
- **Pinned CLI:** `evalshift==0.12.1` by default, overridable.
- **What it adds on top of the CLI:** hosted push, baseline lookup, cross-branch diff, the
  governed policy verdict, one self-updating PR comment, a commit status, and an exit code.

> **Behaviour change — the default gate moved.** `fail-on` now defaults to `policy`: the exit
> code follows hosted EvalShift's verdict on your project's migration policy, not the action's
> own count of regressions in the diff. The two can disagree in both directions. Set
> `fail-on: regression` to keep the previous behaviour. See
> [Gating: the `fail-on` modes](#gating-the-fail-on-modes).

---

## Table of contents

1. [Who this is for](#who-this-is-for)
2. [Prerequisites](#prerequisites)
3. [Quick start](#quick-start)
4. [The EvalShift token](#the-evalshift-token)
5. [Secrets and provider keys](#secrets-and-provider-keys)
6. [Inputs](#inputs)
7. [Outputs](#outputs)
8. [Gating: the `fail-on` modes](#gating-the-fail-on-modes)
9. [What lands on the pull request](#what-lands-on-the-pull-request)
10. [Permissions](#permissions)
11. [How it works, step by step](#how-it-works-step-by-step)
12. [Branch and baseline resolution](#branch-and-baseline-resolution)
13. [Cost control](#cost-control)
14. [Plan limits and the CI preflight](#plan-limits-and-the-ci-preflight)
15. [Recipes](#recipes)
16. [Security model](#security-model)
17. [Limits and known edges](#limits-and-known-edges)
18. [Troubleshooting](#troubleshooting)
19. [Versioning and stability](#versioning-and-stability)
20. [FAQ](#faq)

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
| 3 | Repository secret `EVALSHIFT_TOKEN` | Hosted EvalShift → Settings → API tokens → Service accounts. A scoped service-account key, starting with `es_`. See [The EvalShift token](#the-evalshift-token). |
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
          fail-on: policy # the default; gates on your migration policy
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

## The EvalShift token

### Storing it

`token` must come from a GitHub **encrypted secret** — repository, environment, or
organization. There is no other supported storage.

- **Never commit it.** Not to a config file, not to a `.env` the workflow reads, not as a
  literal `env:` or `with:` value in the workflow YAML. Anyone who can read the repo can read
  those, and deleting the line later does not remove it from git history.
- **Never expose it to `pull_request_target`.** That trigger runs the base repository's
  workflow — with secrets in scope — against a fork's code, so a fork PR can exfiltrate the
  key. Use `pull_request`: it has no secrets on a fork run, and the action fails cleanly on the
  empty `token`.
- **Prefer an environment secret in production repositories.** Scoping the key to a deployment
  environment lets you attach required reviewers and branch restrictions to the credential
  itself, rather than trusting every workflow in the repo with it.

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

Masking is not storage. The action registers the token with GitHub's log masking and redacts it
out of CLI output, but that only protects the job's own logs.

### Least privilege: a service-account key, not a personal token

Mint the token from a **service account** — EvalShift web app → Settings → API tokens →
Service accounts, at `/app/<org-slug>/settings/tokens`. A service account is an org-owned
machine identity, so the credential survives the person who set CI up leaving the team. A
personal token is tied to one membership; when that membership goes, every pipeline using it
goes red. Do not use a personal token for CI.

The scope picker on that page speaks EvalShift's permission keys directly. The action needs
exactly two:

| Scope | What needs it |
| ----- | ------------- |
| `run:create` | `evalshift push` — creating the hosted run and finalizing the upload. |
| `run:read` | `GET /runs/{id}/baseline-compatible` and the diff this action gates on. |

Give the service account the `member` role. A `viewer` holds `run:read` but not `run:create`,
so it can look but never upload.

Two consequences of a correctly-scoped key, both by design:

- **It cannot auto-create the hosted project.** `project:create` is an owner permission, and a
  service account is never an owner. Create the project once in the web app, then set
  `create-project: false` so a wrong project slug reads as a missing project rather than as a
  credential problem.
- **It cannot rewrite the project's gating thresholds.** `evalshift push` sends the
  `thresholds:` block from your `evalshift.yaml` whenever one is present, and rewriting a
  project's gating policy needs the owner-only `policy:configure`. Keep thresholds canonical in
  the web app and out of the config the CI job runs, or the push fails with
  `Project owner role required`.

A denial is self-diagnosing: the action prints the exact permission key the hosted API refused,
plus how to mint a key that holds it, before exiting non-zero.

### Rotating it

1. **Rotate** the key in the web app. The old one keeps working for a 24-hour grace window.
2. **Update** the GitHub secret with the new value.
3. **Confirm** a green run on the new key, then let the old one expire.

In that order there is never a moment when the pipeline holds no valid key. Never swap a key's
secret in place — overlapping keys exist precisely so a running deploy cannot race a credential
change. Keys that stop being used are flagged as stale in the web app; a key you rotated away
from should show up dead there within the grace window.

---

## Secrets and provider keys

The action does **not** manage provider credentials. It passes the job environment through to
the CLI, adding `EVALSHIFT_HOST`, `EVALSHIFT_TOKEN` and `COLUMNS=512` (the last so rich does
not fold the hosted run URL across two lines) and touching nothing else, so set the key as a
job-level `env:` entry and the CLI picks it up.

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
| `evalshift-version` | no | `0.12.1` | Exact CLI version installed from PyPI. Pin this for run-to-run reproducibility across CLI releases. |
| `python-version` | no | `3.12` | Python used to install and run the CLI. Must satisfy the CLI's minimum (3.11 for 0.12.1). |
| `fail-on` | no | `policy` | Gating mode. See [below](#gating-the-fail-on-modes). |
| `branch` | no | auto | Candidate branch name recorded on the hosted run. Auto-detected from the PR head ref, else the pushed ref. |
| `base-branch` | no | auto | Branch to look for a baseline run on. Auto-detected from the PR base ref, else the current ref. Resolving to empty means no baseline is fetched and the check always passes. |
| `create-project` | no | `true` | Whether `evalshift push` may auto-create the hosted project when it doesn't exist. Set `false` to make a missing project a hard failure. |
| `comment` | no | `true` | Whether to create or update the PR comment. Set `false` to keep the commit status but stay out of the conversation. |
| `github-token` | no | `github.token` | Token used for the PR comment and the commit status. Override only to have a bot account post instead of `github-actions`. |
| `repo-private` | no | `${{ github.event.repository.private }}` | Whether this repository is private, used by the [plan preflight](#plan-limits-and-the-ci-preflight). Reported to EvalShift, not verified by it. Override only when the GitHub context doesn't describe the code you're actually evaluating. |

Boolean inputs accept `1`, `true`, `yes`, `on` (case-insensitive). Anything else is false.

---

## Outputs

| Output | Value |
| ------ | ----- |
| `run_url` | Hosted run URL for this run. |
| `diff_url` | Hosted diff URL comparing this run to the baseline. Empty string when no compatible baseline was found. |
| `run_id` | Hosted EvalShift run id — the server-minted id every `/runs/{id}` API route takes. Not the local `r_…` run directory name. |
| `regression_count` | Number of regressed examples in the hosted diff. `0` when there is no baseline. |
| `conclusion` | `success` or `failure`, reflecting `fail-on`. These are GitHub commit-status states, so a policy that *declined* to decide is `success` here — the PR comment and the status description carry the verdict itself. |

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
| `policy` | Hosted EvalShift evaluates the run against the project's migration policy and answers `fail`. **Default.** |
| `never` | Never. Records the run, pushes it, comments — but never blocks the merge. Use while you're still calibrating a suite. |
| `regression` | The hosted diff reports one or more regressed examples in aggregate. |
| `any-slice-regression` | Any slice's pass rate moved down, even when the aggregate is flat or improved. Stricter — catches one slice degrading while the overall number hides it. |

### `policy` — the governed gate

`policy` is the only mode that enforces what you configured. The action calls
`GET /runs/{run-id}/policy-check` and follows the answer; it does not re-implement a single
threshold. The same verdict is what the CLI and the web app show, so a merge blocked in CI is
blocked for the same stated reason everywhere.

That makes it disagree with `regression` in both directions, on purpose:

- A run with regressed examples that still sits inside every budget **passes**. Under
  `regression` it would have failed, and the honest reading is that your policy said this much
  movement is acceptable.
- A run whose aggregate regression count is `0` but which busts a cost, latency or per-slice
  budget **fails**. `regression` never saw that question.

The response also carries the arithmetic — each budget's observed value, its allowance, its
scope, and whether the measurement was conclusive — and every blocking regression behind the
verdict. Both are rendered into the PR comment, so the check explains itself without a trip to
the web app.

#### The four statuses

`policy-check` answers with a closed set of four. Exactly one of them fails the job.

| `status` | `should_fail` | Rendered as |
| -------- | ------------- | ----------- |
| `pass` | `False` | A clean pass. No caveat blockquote. |
| `conditional_pass` | `False` | A pass, with a caveat blockquote and the server's reason. |
| `fail` | `True` | A failure, with the busted budgets and blocking regressions. |
| `inconclusive` | `False` | Undecided — explicitly *not* a pass. |

**`conditional_pass` is a pass.** Every budget held and nothing critical or high regressed, but
something milder did: medium/low regressions, and/or comparisons that scored zero pairs. The
server says so in its own reason — which contains *"not a gate failure"* and ends *"Review
before merging."* — and the action passes that through verbatim. It does not fail the gate, and
it is not treated as undecided: the check is green, with a blockquote saying the run is not
clean and pointing at the reason.

**`inconclusive` has six distinct causes**, carried only by the `reason` string:

1. no policy is configured on the project — there were no budgets to check, and
   `policy` is `null` with `budgets: []`;
2. no policy metrics were recorded for the run at all;
3. nothing was measured — no blocking evaluator scored a single record, so the quality
   budgets are clean by absence rather than by evidence;
4. nothing was comparable — every comparison scored severity `insufficient`;
5. a budget was breached, but on too small a sample to confirm the breach;
6. a slice your policy declares was not measured by this run.

The action never substitutes its own wording for these. It prints the server's `reason`
verbatim in the PR comment, because paraphrasing would collapse six different problems — and
six different fixes — into one sentence on the surface you actually read. It is also why the
list growing (it went from three to six when the gate stopped inventing a default policy)
changes nothing the action has to ship: it never enumerated them in code.

**When the policy declines to answer.** `inconclusive` — and any status outside the four above,
which a newer server may return to an older pinned action — **does not fail the job**, and is
never presented as a pass. The comment and the commit-status description say the gate did not
decide. The `conclusion` output stays `success`, because it is a GitHub commit-status state and
there is no third value; read the comment, not the output, when you care about the difference.

**When the policy check is unavailable.** If it errors, 404s, or has no stored decision for the
run, the action falls back to `regression` gating for that run and announces it in the job log,
the commit-status description, and the PR comment. It does not silently go green — but for that
run the gate is the diff, not your policy.

### The diff-only modes

`any-slice-regression` is not simply "stricter than `regression`" in every case; it's a
different question. A run where the aggregate regression count is above zero but no individual
slice moved down will fail under `regression` and pass under `any-slice-regression`. If you
want both guarantees, run the action twice with different modes (and `comment: "false"` on one
of them), or keep `regression` and rely on slices for diagnosis rather than gating.

**Suggested progression:** start at `never` for a week or two while the suite settles, then move
to `policy` and tune the budgets in the web app until the gate agrees with your own judgement of
which PRs should have been blocked. Reach for `regression` or `any-slice-regression` only when
you deliberately want a diff-shaped question the policy doesn't ask.

When no compatible baseline run exists on the base branch, there's nothing to compare against:
the diff-based modes pass, `regression_count` is `0`, and the PR comment says so explicitly.
`policy` still asks the server for a verdict — the policy is about this run, not about the
comparison.

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
> **Policy decision:** `fail` (from `project_policy`)
> **Why:** pass-rate drop of 4.2 pts exceeds the allowed 2.0 pts
>
> ### Policy budgets
>
> | Budget | Scope | Observed | Allowed | Result |
> | --- | --- | ---: | ---: | --- |
> | pass_rate_drop | overall | 0.042 | 0.02 | fail |
> | cost_increase | overall | 0.1 | 0.25 | pass (not confident) |
>
> ### Blocking regressions
>
> | Prompt | Evaluator | Slice | Severity | Score delta |
> | --- | --- | --- | --- | ---: |
> | p-refund | accuracy | safety_refusals | critical | -0.4 |
>
> **Pass-rate movement:** -12 pts
>
> | Slice | Pass-rate delta |
> | --- | ---: |
> | safety_refusals | -25 pts |
> | tool_selection | -8 pts |

The two policy sections appear under `fail-on: policy` only; the other modes never ask for a
verdict, so there is nothing honest to render.

Budgets are listed failing-first and capped at 12 rows, blocking regressions at 10, with a
line naming how many were omitted — a policy with a per-slice budget for fifty slices should not
bury the diff under its own table. Each row names the `scope` it was judged at: `overall`, or
the slice name for a per-slice budget, so one budget name evaluated across ten slices still
reads unambiguously. When the cap hides failing budgets, the omission line says how many
(*"8 more budgets not shown, 8 of them failing"*) — with per-slice budgets there can be more
failures than fit, and a bare count would read as "the rest were fine". `pass (not confident)`
marks a budget the server reported as `conclusive: false`: the measurement was too noisy to
tell, and rendering it as a clean pass would be a lie of omission.

A run where nothing was comparable comes back with `budgets: []` and `status: "inconclusive"`.
Both tables are then omitted entirely rather than rendered empty, and the server's reason is
what the reader gets instead.

A blockquote above the tables states the caveat on a `conditional_pass`, and states plainly
when the policy could not decide or when the policy check could not be reached.

Up to five regressed slices are listed, worst first. When nothing regressed you get a single
`No regressed slices` row. Percentages are rounded for display only — gating uses the raw
values, so a slice can appear as `0 pts` and still count as a regression.

Without a baseline, the slice table is replaced by a single line: *No compatible baseline run
was found on the base branch.* The policy sections still render.

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
2. **Preflight.** Asks hosted EvalShift whether this job is covered by the org's plan, before
   a single model call. A `402` stops the job here; anything else lets it continue. See
   [Plan limits and the CI preflight](#plan-limits-and-the-ci-preflight).
3. **Run.** `evalshift all --yes --config <config> --suite <suite>` in the workspace root.
   This is the full local pipeline: doctor → run → evaluate → analyze → report. Artifacts land
   in `.evalshift/runs/<run-id>/`, including the self-contained `report.html`.
4. **Push.** `evalshift push <run-id>` uploads the run bundle to hosted EvalShift, creating the
   project if `create-project` allows it. Git metadata from the runner environment travels with
   the bundle so the server can pair this run with base-branch runs later.
5. **Find a baseline.** Asks the hosted API for the latest compatible run on the base branch.
   "Compatible" is a server-side judgement — a suite that changed shape can't be diffed against
   an older one.
6. **Fetch the diff.** Pulls aggregate and per-slice deltas from the hosted API.
7. **Ask the policy gate.** Under `fail-on: policy`, `GET /runs/{id}/policy-check` returns the
   server's verdict on this run against the project's migration policy, with the budget-by-budget
   arithmetic behind it. Skipped in the other modes.
8. **Report.** Writes the five outputs, upserts the PR comment, sets the commit status.
9. **Gate.** Exits non-zero when the gate says so — the policy verdict under `fail-on: policy`,
   the diff otherwise.

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

## Plan limits and the CI preflight

Hosted EvalShift plans limit a few things that CI runs into: runs per month and how many runs
an org may have in flight at once. (Private-repo CI is included on every plan; the preflight
still reports the repository's visibility.) Discovering one of those halfway through a
suite means paying for the model calls and getting nothing, so the action asks first.

**What it does, before installing anything of yours or spending a credit:**

1. Reads `project: <org>/<project>` from your config file — the same key `evalshift push` uses.
2. Resolves that project through `GET /orgs/<org>/projects`.
3. Calls `POST /projects/<id>/ci-preflight` with `{"repo_private": <bool>, "parallelism": 1}`.

**A 402 fails the job immediately.** You get, in three places:

- an `::error::` annotation at the top of the job,
- a step summary block naming the plan, what was blocked, any limit and its reset date, and an
  upgrade link,
- on a pull request, the usual EvalShift comment (same marker, so it replaces the previous one)
  carrying the same message.

Every word of it comes from the server. The action never decides what a plan covers — it can't,
and a client that guesses at entitlements is a client that tells people the wrong thing after
the next pricing change.

**Everything else is fail-open.** A 5xx, a timeout, a DNS failure, a project that doesn't exist
yet, a token without `project:read` — all of them print `warning: plan preflight skipped: ...`
and the run continues. Billing fails closed; infrastructure fails open. An EvalShift outage
must not break your CI, and the server still enforces every limit when the run is uploaded, so
nothing escapes by skipping the preflight.

**The preflight is skipped entirely** when the config has no top-level `project:` key — there's
nothing to resolve before the CLI builds the bundle.

### About `repo-private`

It defaults to `${{ github.event.repository.private }}` and is *asserted*, not verified: the
server has no view of your GitHub repository. Two consequences worth knowing:

- The server records the first `true` permanently. A project that has ever reported private
  stays private, so reporting `false` afterwards changes nothing.
- Overriding it to `false` on a private repository is a licence violation, not a clever trick.
  If your workflow genuinely evaluates public code from a private repository, set it explicitly
  and be prepared to explain it.

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
- **The hosted key is a scoped machine credential.** Mint it from a service account with only
  `run:create` and `run:read`, store it as an encrypted secret, and rotate it through the
  overlapping-key flow. Details and rationale: [The EvalShift token](#the-evalshift-token).
- **Dependencies.** The runtime helper is stdlib-only. `pip-audit` runs in this repo's own CI.

---

## Limits and known edges

Worth knowing before you rely on this in anger:

- **The PR comment lookup reads only the first page of comments.** On a very long PR thread the
  EvalShift comment can fall off page one, and a second comment gets created instead of the
  first being updated.
- **The comment marker and status context are global constants.** Parallel invocations on the
  same PR overwrite each other. One commenting invocation per PR.
- **The run that gets pushed is the newest directory under `.evalshift/runs`.** If a step
  between the run and the push touches an older run directory's mtime, the wrong run gets
  pushed. In a normal workflow this never happens.
- **The hosted run URL is parsed from CLI stdout, and the hosted run id is read out of its
  `/app/{org}/{project}/runs/<uuid>` path.** The action anchors that whole shape, so a change
  to either how the push result is printed or how the web app routes runs would break it. The
  repo's `cli-contract` CI job guards flag renames but not output shape; a URL the action
  cannot read a run id out of fails the step rather than gating on a guess.
- **No retries on hosted API calls.** A 30-second timeout, one attempt. A transient hosted
  outage fails the step rather than silently passing — deliberate, but it means a flaky network
  reads as a failed job.
- **`fail-on` decides the exit code, not whether the run happened.** Even at `never`, the run
  executes, costs money, and pushes.
- **A policy that cannot decide does not fail the job.** `inconclusive` — and any status
  outside the four the server defines, which a newer server could return to an older pinned
  action — is reported loudly and gated as a non-failure. If you want undecided to block a
  merge, that is a policy change on the server, not an action input.
- **`conditional_pass` merges.** It is a pass by design, not a near-miss the action rounds
  down. The check goes green, the comment carries the caveat, and nothing stops the merge. If
  medium/low regressions should block, tighten the policy server-side — the action will not
  second-guess the verdict it was given.
- **An unreachable policy check degrades to `regression`.** The action says so in three places
  rather than going quietly green, but for that run the gate is the diff, not your policy.
- **The plan preflight reads `project:` with a regex, not a YAML parser.** The runtime helper
  has no dependencies. A top-level `project: org/name` is found; anything more exotic isn't,
  and the preflight is skipped rather than guessed at. The server still enforces the limit at
  upload time.
- **A denied preflight fails the job at `fail-on: never` too.** `fail-on` governs regressions;
  a plan that doesn't cover the run is a different question, and the run never happens.

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

### `could not read a server run id out of the hosted run URL: ...`

The push printed a URL, but the action could not find an `/app/{org}/{project}/runs/<uuid>`
path in it. Every hosted call afterwards is keyed on that id, so the action stops rather than
gate on a guess. Usually a CLI version mismatch — an older CLI printed the local `r_…` run
directory name in the URL instead of the server-minted id. Check the printed URL in the step
log and pin a newer `evalshift-version`.

### HTTP 401 from the hosted API

Bad, revoked, or expired `EVALSHIFT_TOKEN`, or the wrong `host`. Verify with `evalshift whoami`
locally using the same token. A key past its rotation grace window authenticates as nobody.

### `The EvalShift token is missing the '<key>' permission`

The key authenticated fine but its scopes (or its service account's role) don't cover what the
step needed. Widen the scope on the existing key, or mint one that holds it — see
[Least privilege](#least-privilege-a-service-account-key-not-a-personal-token). The two the
action always needs are `run:create` and `run:read`.

### `cannot auto-create project: this token must have owner access to the org`

A service-account key cannot create projects; `project:create` is owner-only. Create the project
in the web app and set `create-project: false`.

### `Project owner role required`

`evalshift push` tried to rewrite the project's gating thresholds, which needs the owner-only
`policy:configure`. Remove the `thresholds:` block from the config the CI job runs and manage
thresholds in the web app.

### `warning: could not upsert PR comment: HTTP 403`

Missing `pull-requests: write` / `issues: write`, or a fork PR with a read-only token. The
gating still works — only the comment is lost.

### The job failed before running anything, with a plan message

The [CI preflight](#plan-limits-and-the-ci-preflight) got a `402`: the org's plan doesn't cover
this run. The annotation and the step summary name the limit and link to the billing page. The
usual causes are the monthly run quota and the parallelism cap. Nothing was run and nothing
was charged.

### `warning: plan preflight skipped: ...`

The preflight couldn't get an answer, so the run continued — the intended behavior. Common
causes: the project doesn't exist on hosted EvalShift yet (the first push creates it), the
token lacks `project:read`, or hosted EvalShift is unreachable. The server still enforces plan
limits when the run is uploaded, so this warning never means a limit was bypassed.

### `warning: hosted policy check ...; falling back to fail-on: regression`

Under `fail-on: policy` the action could not get a verdict for this run — the endpoint errored,
answered `404`, or holds no stored decision for the run. The job still gated, but on the diff
rather than on your policy, and the PR comment carries the same warning. A persistent `404` on
runs that *do* exist usually means the project has no migration policy configured, so there is
nothing for the server to decide against.

### The check is always green

In order of likelihood: under the default `fail-on: policy`, the project's migration policy is
permissive enough that nothing has busted a budget yet (the comment shows the budget arithmetic —
if every row passes with room to spare, tighten them in the web app); no baseline run exists on
the base branch yet (add the `push` trigger to `main` and merge once); `fail-on` is `never`; or
`base-branch` resolved to an empty string.

### Two EvalShift comments on one PR

Either two action invocations are commenting, or the original comment fell off the first page of
the comments API on a long thread.

### The job hangs with no output

It doesn't — output is buffered per command and printed when each finishes. A slow suite is
silent while it runs.

### `pip install evalshift==0.12.1` fails

`python-version` is below the CLI's minimum. EvalShift 0.12.1 needs Python 3.11+.

### Costs are higher than expected

The runner cache is cold every run. Narrow the trigger with `on.pull_request.paths`, shrink the
CI suite, or swap LLM-judge evaluators for structural ones in a CI-specific config.

---

## Versioning and stability

Pin to `@v0` to track the latest v0.x, or to an exact tag such as `@v0.3.0` for a fully
reproducible workflow. The `evalshift-version` input pins the CLI separately — pin both if you
want a workflow that behaves identically six months from now.

This repo's CI includes a `cli-contract` job that installs the exact pinned CLI version and
runs `scripts/cli_contract.sh` to assert the command-line surface the action depends on still
exists. It costs nothing (no API keys, no model credits) and it's the early-warning system for
CLI drift. A separate manual `dogfood` workflow exercises the whole path — install, run, hosted
push, baseline lookup, outputs — against a four-example fixture project; it's manual because it
spends real credits.

The `evalshift-version` default in `action.yml` is the single source of truth for the pinned
CLI. Every other mention (README, this document, `llms-full.txt`) is asserted equal by
`tests/test_pin_consistency.py`, and `scripts/bump_cli_pin.py <new-version>` rewrites all of
them plus the action's patch version in `pyproject.toml`. The pin is kept current by
`.github/workflows/bump-cli-pin.yml`: it runs daily (polling PyPI for the latest release), on
`workflow_dispatch` with an optional `version` input, or on `repository_dispatch` of type
`evalshift-cli-release`. Before opening anything it checks the target's `requires-python`
against the `python-version` default, installs the target CLI and runs the contract script, then
runs the bump script and the test suite; only then does it open a PR on branch
`bump/evalshift-<version>` titled `chore(pin): evalshift <old> → <new>`. The optional
`BUMP_PR_TOKEN` secret (a fine-grained PAT with `contents` and `pull-requests` write) lets the
normal CI run on that PR — a PR opened with `GITHUB_TOKEN` does not trigger `ci.yml`; without the
secret the PR still opens, pre-validated.

Merging a PR that bumps `version` in `pyproject.toml` *is* the release — there are no manual
tags. On every push to `main`, `.github/workflows/release.yml` creates `v<version>` on the merge
commit if it does not exist yet, force-moves the floating `v0` tag to the same commit (only `v0`
is ever moved), and publishes a GitHub Release with generated notes since the previous `v0.*`
tag; ordinary merges stop green. So a merged bump PR reaches every `@v0` consumer immediately.
The workflow refuses a major other than `0` — a 1.x release needs a `v1` tag and a docs change.

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
Three common reasons. The default `fail-on: policy` gates on your migration policy, and a
regression that stays inside its budgets is a pass by design; `fail-on: never` is set; or there
was no compatible baseline so nothing was compared. The comment states which.

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
