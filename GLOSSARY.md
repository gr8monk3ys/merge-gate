# merge-gate

A policy engine that auto-merges bot-produced PRs across a fleet of repos only when a computation over GitHub's data proves the diff safe, and routes everything else to a human. It exists because agent judgement about PRs drifted and computation did not.

## Language

### What gets judged

**Gate**:
The decision procedure in `merge_gate.py`: four conditions, all required, that a PR must meet before it is merged.
_Avoid_: bot, merger, auto-merge (auto-merge is the GitHub feature the gate refuses to use)

**Sweep**:
One run of the gate over every open PR in the Fleet, ending in verdicts and, outside a dry run, merges.
_Avoid_: scan, batch

**Machine-produced PR**:
A PR labelled `automated`, on an `auto/*` head branch, or opened by Dependabot. Only these are ever considered.
_Avoid_: bot PR, agent PR

**Shape**:
The category a diff falls in, computed from the changed paths alone: `lockfile-only`, `tests-only`, `data-artifact-only`, `ci-fix`, `dependency-bump`, `content`, `mixed`, `empty`, or `unreadable`.
_Avoid_: type, kind, category

**Allowlist**:
The set of Shapes that may auto-merge: `lockfile-only`, `tests-only`, `data-artifact-only`, `ci-fix`, `dependency-bump`.

**Candidate shape**:
A Shape that is only provisionally named (`ci-fix-candidate`, `dependency-bump-candidate`) until `promote_candidate` judges the versions or added lines.

**Mixed**:
A diff with any path outside the matched Shape's patterns. There is no partial credit; Mixed always routes to a human.

**Content veto**:
The absolute block on auto-merge when any changed path is editorial content, whatever the Shape.
_Avoid_: content shape

**Bump kind**:
The size of a version delta (`patch`, `minor`, `major`) read from the PR body; a `0.x` minor counts as breaking.
_Avoid_: semver level

### What "green" means

**Correctness check**:
A required check that gates the code still working (test, build, lint, typecheck), recognised by name or declared in `repos.yml` `correctness_checks`.
_Avoid_: required check (a security scan can be required without being a Correctness check)

**Judged head**:
The exact head commit the gate evaluated; the merge is issued with `--match-head-commit` so GitHub refuses it if the head has moved.
_Avoid_: latest commit

**Arm**:
To enable GitHub auto-merge on a PR. The gate never arms, and disarms any arm it finds.

**Stale base**:
A PR whose checks ran against a base branch that has since moved, so its green no longer applies.

**Drift**:
A repo that permits auto-merge but has no Correctness check required on its base branch.

**Verify evidence**:
CI-reported test counts at head and base, from the repo's `verify_workflow`, that license a tests-only (or opted-in) PR.

### Safety and record

**Verdict**:
The gate's decision on one PR in one Sweep: `MERGED`, `WOULD_MERGE`, `STALE`, `ROUTED`, `SKIPPED`, `DRIFT`, `HELD` or `NO_VERDICT`.
_Avoid_: result, status

**Circuit breaker**:
A repo-level reason a green, allowlisted PR is still HELD: paused, an unacknowledged revert, the daily merge cap, or a red base.

**Ledger**:
The append-only JSONL file of Verdict rows (and pause events) that the rate and revert breakers read back.
_Avoid_: log, history

**Watchdog**:
`ci_watchdog.py`, which reports repos whose required check is red at the default branch.

### The fleet

**Fleet**:
The repos under the configured Owners that the gate sweeps.

**Owner**:
A GitHub account (user or org) the Fleet spans, set by `GATE_OWNERS`.

**Registry**:
`repos.yml`, the operator's per-repo configuration (opt-ins, `may_skip`, caps, pauses).
_Avoid_: config

**Reviewed path**:
The second way to merge: a PR whose Shape is off the Allowlist merges in an opted-in repo when a `merge-gate/review` commit status is `success` on its judged head.

**Transport**:
The layer in `gh_transport.py` that issues `gh`-shaped argv through the `gh` binary or REST when `gh` cannot answer.
