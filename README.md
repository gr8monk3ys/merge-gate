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

Three conditions. All required. No overrides.

1. **The diff's shape is on the allowlist** — computed from the changed file
   paths, never from the title, the branch name, a label, or the loop that
   claims to have produced it.
2. **The repo has at least one required status check that actually runs on
   pull requests.** A shape match on an ungated repo does not merge.
3. **Every required check is green at the exact commit that was judged**, and
   that commit is what gets merged (`gh pr merge --match-head-commit`).
   Nothing is ever *armed*: an arm is standing permission to merge future
   content on the strength of a judgement about past content.

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

## Quickstart

Needs Python ≥ 3.11 and an authenticated [`gh`](https://cli.github.com/).

```sh
gh auth status
export GATE_OWNERS="you,your-org"      # every account the fleet lives under

python3 merge_gate.py                  # report only — the default
DRY_RUN=0 python3 merge_gate.py        # merge judged heads, label the rest
```

The report separates *would merge* / *routed to review* / *skipped (ungated
repo)* / *no verdict (GitHub did not answer — retry, not a finding)*.

| Variable | Default | Meaning |
|---|---|---|
| `GATE_OWNERS` | *(required)* | Comma-separated users/orgs. An owner that yields zero repos aborts the sweep rather than silently shrinking the fleet. |
| `DRY_RUN` | `1` | `0` to merge and label. |
| `INCLUDE_BOTS` | `1` | `0` to ignore Dependabot. Bots are roughly four fifths of a real queue; leaving them out is how a full queue reports as empty. |
| `ONLY_PUBLIC` | unset | `1` to skip private repos — for GitHub's capped private-repo Actions minutes, not for safety. |

## The tools

| Script | Question it answers |
|---|---|
| `classify_pr.py` | What shape is each open PR, and would it merge? Pure classifier; `--validate` runs the self-tests plus any live fixtures you add. |
| `merge_gate.py` | Merge what earns it, at its judged head. Label the rest `needs-review`. |
| `ci_watchdog.py` | Is each repo's **default branch** still green — and since when? A red default branch blocks every merge in that repo and looks identical, from outside, to a repo with nothing to do. One repo sat that way for 37 days holding 15 mergeable PRs. |
| `verify-gates.py` | Does every *required* check actually fire on pull requests? A required check that only runs on push never goes green on a PR, so nothing can ever merge. |

Optional: `repos.yml` (see `repos.example.yml`) declares `data_dir` opt-ins.
Nothing else is configuration; the policy is three constants at the top of
`classify_pr.py`, and changing what merges is a one-line, deliberate edit.

## Tests

```sh
pip install ".[dev]"
pytest                          # pure: no network, no gh
python3 classify_pr.py --validate
```

The tests are the policy written as statements — *a group is as risky as its
worst member*, *the sentence period is not part of the version*, *only
"dirty" is a conflict verdict*. Read `tests/test_policy.py` as the spec.

## Provenance

Extracted on 2026-08-28 from the private control plane that runs it on a
schedule. The numbers in the comments are real; the repos named are the
author's. The private copy is vendored, so the two can diverge — `diff` is the
sync check until the control plane consumes this package directly.

GPL-3.0-or-later.
