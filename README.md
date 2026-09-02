# merge-gate

Auto-merge bot PRs across a fleet of repositories — only when a computation,
not a person, says the diff is safe.

Built for a fleet of ~60 repos where Dependabot and scheduled coding agents
open more pull requests than anyone can read. Review was the bottleneck, and
the first attempt to fix it — an agent that "enabled auto-merge on anything
green with an eligible shape" — judged shape by eye, armed a 197-file diff to
land unreviewed, and later let a major version merge 130 seconds after it was
force-pushed into a PR that had been judged as a minor. Every rule in this
repo is the fix for something like that. [`DECISIONS.md`](DECISIONS.md) tells
each story.

## The rule

Four conditions. All required. No overrides.

1. **The diff's shape is on the allowlist** — computed from the changed file
   paths, never from the title, the branch name, a label, or the loop that
   claims to have produced it.
2. **The repo has at least one required status check that actually runs on
   pull requests.** A shape match on an ungated repo does not merge.
3. **Every required check is green at the exact commit that was judged**, and
   that commit is what gets merged (`gh pr merge --match-head-commit`).
   Nothing is ever *armed*: an arm is standing permission to merge future
   content on the strength of a judgement about past content.
4. **Those checks ran against the base branch's current head.** A PR's
   `base.sha` is the base its checks saw; if the branch has moved since —
   by anyone, or by this sweep's own previous merge — the green is stale
   and the PR waits. One merge per base branch per sweep; a stale
   Dependabot PR is asked to rebase, a stale loop PR routes to review.

| Shape | Merges when | Never merges |
|---|---|---|
| `lockfile-only` | only regenerated lockfiles changed **and** every version delta inside is patch/minor | a major hiding in `uv.lock` |
| `dependency-bump` | manifest + lockfile, every delta patch/minor (read from the PR body — the title of a group bump names no versions) | any group containing a major, a `0.x` minor, or a SHA-only bump |
| `tests-only` | every path is a test file | one source file alongside forty tests — there is no partial credit |
| `data-artifact-only` | every path is inside a directory the repo has explicitly opted in via `data_dir` | anything, if the repo never opted in |
| `ci-fix` | every added line matches an explicitly reviewed whitelist of action bumps | a workflow edit that merely *reads* like a fix |
| `content` / `mixed` / `empty` / `unreadable` | — | always routed to a human |

A **content veto** sits on top of all of it: if any changed path is editorial
content, the PR does not auto-merge whatever its shape. And an *unreadable*
diff vetoes — "I could not check" never resolves to "there is nothing there".

## What it never does

- Never runs `gh pr merge --auto`. It **disarms** any arm it finds.
- Never merges a PR a human opened, whatever the diff looks like.
- Never merges a draft.
- Never renders a failed API read as a verdict. A PR GitHub did not answer
  about is reported in its own section, not filed as decided.
- Never reports a drained queue it did not fully enumerate. Search endpoints
  cap silently; it lists per repo and cross-checks the total.

## Install

```sh
pip install "merge-gate @ git+https://github.com/gr8monk3ys/merge-gate@v0.3.0"
```

That puts three console scripts on `PATH` — `merge-gate`, `ci-watchdog`,
`classify-pr` — so the gate runs from anywhere, not only from a checkout.
Running the files directly still works and is equivalent.

## Quickstart

Needs Python ≥ 3.11. An authenticated [`gh`](https://cli.github.com/) is used
when it is present and working; when it is not, the same calls go over the
REST API instead (see *Transport* below), so no binary is strictly required.

```sh
export GATE_OWNERS="you,your-org"      # every account the fleet lives under

merge-gate                             # report only — the default
DRY_RUN=0 merge-gate                   # merge judged heads, label the rest
```

The report separates *would merge* / *routed to review* / *skipped (ungated
repo)* / *no verdict (GitHub did not answer — retry, not a finding)*.

| Variable | Default | Meaning |
|---|---|---|
| `GATE_OWNERS` | *(required)* | Comma-separated users/orgs. An owner that yields zero repos aborts the sweep rather than silently shrinking the fleet. |
| `DRY_RUN` | `1` | `0` to merge and label. |
| `INCLUDE_BOTS` | `1` | `0` to ignore Dependabot. Bots are roughly four fifths of a real queue; leaving them out is how a full queue reports as empty. |
| `ONLY_PUBLIC` | unset | `1` to skip private repos — for GitHub's capped private-repo Actions minutes, not for safety. |
| `GATE_REPOS_YML` | `./repos.yml` | Path to the optional `repos.yml`. Absolute is safest — installed as a package there is no "next to the source file". |
| `GH_TRANSPORT` | `auto` | `gh` forces the binary, `rest` forces the REST API. `auto` uses the binary only when it can actually authenticate. |

## The tools

| Script | Question it answers |
|---|---|
| `classify_pr.py` | What shape is each open PR, and would it merge? Pure classifier; `--validate` runs the self-tests plus any live fixtures you add. |
| `merge_gate.py` | Merge what earns it, at its judged head. Label the rest `needs-review`. |
| `ci_watchdog.py` | Is each repo's **default branch** still green — and since when? A red default branch blocks every merge in that repo and looks identical, from outside, to a repo with nothing to do. One repo sat that way for 37 days holding 15 mergeable PRs. |
| `verify-gates.py` | Does every *required* check actually fire on pull requests? A required check that only runs on push never goes green on a PR, so nothing can ever merge. (A script, not an importable module — it is not installed.) |
| `gh_transport.py` | Can this environment reach GitHub at all, and by which route? `--whoami` answers; `--difftest` proves the two routes agree. |

Optional: `repos.yml` (see `repos.example.yml`) declares `data_dir` opt-ins;
it is looked for at `GATE_REPOS_YML`, else `repos.yml` in the working
directory.
Nothing else is configuration; the policy is three constants at the top of
`classify_pr.py`, and changing what merges is a one-line, deliberate edit.

## Transport: the gate must run where `gh` does not

A scheduled runner executed the gate 15+ times over three weeks, reported
*"GitHub access is not enabled for this session"* each time, and skipped.
Every script shells out to `gh`, so the gate had not declined to arm anything
— it had never run, and each "queue drained" was a report about a sweep that
did not happen.

`gh_transport.run(argv)` takes the same `gh` argv the scripts already build
and issues it against the REST API when the binary cannot authenticate. Call
sites are unchanged, deliberately: those argv strings encode detail that is
easy to lose by hand (`--paginate` with a line-per-record jq,
`--match-head-commit`, gh's uppercase `visibility`). Credentials come from
`GH_TOKEN`/`GITHUB_TOKEN`, then `gh auth token`, then `git credential fill` —
the last being the same question git asks before a push, so in a runner that
can push, this can read.

**Only the argv forms this package issues are modelled. An unmodelled form
exits nonzero rather than approximating** — every caller already reads that as
"GitHub did not answer" and fails safe, whereas a plausible wrong answer would
be filed as a fact about a pull request.

```sh
python3 gh_transport.py            # jq-subset selftest, no network
python3 gh_transport.py --whoami   # which transport, is there a credential
DIFFTEST_REPO=you/repo python3 gh_transport.py --difftest   # both routes, live
```

## Tests

```sh
pip install ".[dev]"
pytest                          # pure: no network, no gh
python3 classify_pr.py --validate
```

`--difftest` is deliberately **not** in CI: it needs a credential and a real
fleet to compare against. It is the operator's check, run before trusting the
REST route.

The tests are the policy written as statements — *a group is as risky as its
worst member*, *the sentence period is not part of the version*, *only
"dirty" is a conflict verdict*. Read `tests/test_policy.py` as the spec.

## Provenance

Extracted on 2026-08-28 from the private control plane that runs it on a
schedule. The numbers in the comments are real. The control plane vendored a
copy for a month and the two drifted; as of 0.2.0 it installs this package as
a dependency instead, which is why the entry points and the transport live
here rather than there.

GPL-3.0-or-later.
