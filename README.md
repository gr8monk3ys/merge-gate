# merge-gate

Auto-merge bot PRs across a fleet of repositories — only when a computation,
not a person, says the diff is safe.

Built for a fleet of ~80 repos where Dependabot and scheduled coding agents
open more pull requests than anyone can read. Review was the bottleneck, and
the first attempt to fix it — an agent that "enabled auto-merge on anything
green with an eligible shape" — judged shape by eye, armed a 197-file diff to
land unreviewed, and later let a major version merge 130 seconds after it was
force-pushed into a PR that had been judged as a minor. Every rule in this
repo is the fix for something like that. [`DECISIONS.md`](DECISIONS.md) tells
each story.

## The rule

Four conditions. All required. No overrides. (Then the circuit breakers
below get a veto.)

1. **The diff's shape is on the allowlist** — computed from the changed file
   paths, never from the title, the branch name, a label, or the loop that
   claims to have produced it.
2. **The repo has at least one required status check that actually runs on
   pull requests, and at least one of them is a correctness gate** — a
   test/build/lint/typecheck job, judged by name or declared in `repos.yml`
   `correctness_checks`. A shape match on an ungated repo does not merge; a
   repo with auto-merge on and only a security scan required is **DRIFT**.
3. **Every required check is green at the exact commit that was judged** —
   the newest run of each name, across every page of check runs; `skipped`
   counts only for names the repo lists in `may_skip`; still-running is not
   green — and
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
| `tests-only` | every file is a **brand-new** test file (status `added` in the compare of exactly the judged base...head), none is harness/config, none shares a name with an existing path, and none contains a skip/xfail/`.only`/`pytestmark`/`SkipTest`/`__test__` marker or a duplicate test name — **and** the repo opted in (`allow_tests_only`) **and** CI's test counts show more tests ran at head than at base (*Test-count evidence* below) | one source file alongside forty tests — there is no partial credit; **any change to an existing test file** (modified, removed, renamed, copied) routes to a human |
| `data-artifact-only` | every path is inside a directory the repo has explicitly opted in via `data_dir` | anything, if the repo never opted in |
| `ci-fix` | every added line matches an explicitly reviewed whitelist of action bumps | a workflow edit that merely *reads* like a fix |
| `content` / `mixed` / `empty` | only on the reviewed path below | without the repo's opt-in, always routed to a human |
| `unreadable` | — | always routed to a human |

A **content veto** sits on top of all of it: if any changed path is editorial
content, the PR does not auto-merge whatever its shape. And an *unreadable*
diff vetoes — "I could not check" never resolves to "there is nothing there".

## The reviewed path

The allowlist answers "is this diff safe by its shape". A feature PR never
is, so it waits for a person. A repo may name a second answer:
`allow_reviewed: true` in `repos.yml`. Then a PR in the gate's queue whose
shape is not on the allowlist merges when **all** of these hold:

- everything a shaped PR needs: not a draft, auto-merge allowed, a required
  correctness check, every required check green, a fresh base that is not
  red, no breaker;
- the newest `merge-gate/review` **commit status** on the exact head the
  gate judged is `success`. A status belongs to a commit, so a push after the
  review leaves the new head unreviewed, and `--match-head-commit` merges
  that head or nothing.

`allow_reviewed_content: true` extends it to PRs the content veto stops, and
is read only where `allow_reviewed` is also true. An unreadable diff is never
reviewable. The ledger row carries `review` (the state read) and its reason
reads `mixed (reviewed); green: …`.

The gate does not review; it reads a verdict. Who posts the status, and how
hard they looked, is the operator's to arrange. Anyone with push access can
post one, the PR's own author included, so this is a separation of runs, not
of credentials.

## Circuit breakers

A green, allowlisted PR is still **HELD** — its own report section and
ledger decision, never merged, applied in dry runs too — when:

- the repo is paused in `repos.yml` (`merge_gate_paused: true`);
- a **revert of a gate merge** was found on its base branch. Each sweep
  starts by scanning every base branch the gate merged into in the last 14
  days for `This reverts commit <sha>` naming a gate merge, or GitHub's
  `Reverts owner/repo#N` naming a gate-merged PR, and records a pause in the
  ledger. The pause clears **only** when that revert's sha is listed in the
  repo's `merge_gate_ack_reverts`; time passing does not clear it. A repo
  that could not be scanned this sweep is held for the sweep;
- the repo already took `MERGE_GATE_MAX_PER_DAY` (default **3**; per repo
  `merge_gate_max_per_day`) real gate merges in the last 24 hours, counted
  from the ledger plus this sweep's own merges;
- any correctness check on the base branch's head is red (newest run per
  name; `cancelled` does not count).

## Test-count evidence

No scan of a new test file's text can prove it doesn't end the run early
(`os._exit` reached through `__import__`, `importlib`, `setattr`, ...). A
count of what ran can: if a new file stops the run, fewer tests pass. So a
repo's CI uploads an Actions artifact named `verify-report` holding one file,
`verify-report.json`:

```json
{"schema": 1, "repo": "owner/name", "commit": "<tested sha>",
 "stages": [{"stage": "unit", "tests": 120, "passed": 118, "failed": 0, "skipped": 2,
             "passed_ids": ["tests/test_a.py::test_one", "..."]}]}
```

from one workflow, declared per repo in `repos.yml` as
`verify_workflow: .github/workflows/ci.yml`, on every `pull_request` (`commit`
= the PR head sha, not the `refs/pull/N/merge` sha) and every `push` to the
default branch (`commit` = the pushed sha). Without `verify_workflow` there is
no evidence at all (`VERIFY — no evidence: verify_workflow is not set ...`).

**Provenance.** For a PR head the gate takes the newest completed run of
exactly that workflow path with event `pull_request` on that `head_sha`; for
the base, the newest completed `push` run of it on the base branch. Only that
one run counts: it must have concluded `success` and carry exactly one
`verify-report` artifact. If it failed, was cancelled, or has no (or an
invalid) report, that is *no evidence* — an older run is never used instead.
Other workflows' artifacts, `workflow_dispatch` runs and the like are ignored.
The run id, workflow path and event go into the ledger.

**Validation** is strict — schema `1`, this repo, exactly this commit, stages
non-empty and uniquely named, counts non-negative integers with
`passed + failed + skipped == tests`. `passed_ids` is optional per stage; when
present it must be a list of non-empty strings, sorted (Python `sorted()`,
i.e. by code point) and unique, with exactly `passed` entries. More than
200,000 IDs in one report is no evidence. Anything else is *no evidence*, with
the reason. Reports are read once per commit per sweep, and only for a PR
whose required checks are already green.

A **tests-only** PR (in a repo with `allow_tests_only: true`) merges only
with evidence for both its head and the base its checks ran against, and
only when: no test failed at head; every stage the base ran is present at
head; per stage, `tests` and `passed` did not drop and `skipped` did not rise;
where both head and base list `passed_ids` for a stage, every ID that passed
at base passed at head; and the total grew by at least the test cases the diff
adds (`def test…` in Python, `it(`/`test(` in JS/TS) — or by at least one when
they cannot be counted. With IDs, that growth is new IDs: base IDs all pass at
head and there is one ID per pass. Otherwise it routes with the reason, e.g.
`VERIFY — stage e2e: tests 70 -> 64 at head` or
`VERIFY — stage unit: tests/test_a.py::test_one passed at base but not at head`.
Without IDs a swap (one old test stops running, one new one starts) leaves the
counts unchanged, so emit `passed_ids` when the runner can.

`require_verify_evidence: true` in `repos.yml` applies the first two
conditions (no failure at head, no vanished stage) to **every** shape the gate
merges in that repo. Either way the head and base run, workflow, event and
counts (never the IDs) go into the ledger row's `artifacts.verify`.

**What this does not defend against.** The report is produced by CI that
runs the PR's own code: a `pull_request` run executes whatever the PR adds,
and that code can write any report it likes, or rewrite the step that writes
it. The evidence defends against *accidental* weakening — an agent's new test
file that ends the run early, a test that quietly stopped running — not a
deliberately malicious PR. That is why tests-only auto-merge stays opt-in per
repo (`allow_tests_only`), and why the report should be computed from the
runner's own result files (JUnit XML, `--json`) in a step after the tests,
not from anything the tests themselves write.

## Merge ledger

Every verdict — merged, would-merge, stale, routed, skipped, drift, held,
no verdict — appends one JSON line to `MERGE_GATE_LEDGER` (default
`~/.local/state/fleet/merge-gate-ledger.jsonl`; `off` disables it, and a
real run then refuses to start with exit `4`, because the breakers above
read this file). A detected revert appends a `pause` row. Rows carry the
repo, PR, judged head and base sha, shape, `tier` (the PR's bump severity,
when it declares one), each required check's name/conclusion/app,
`green_by_skip`, changed paths, `source` (dependabot / loop / other), the
decision and reason, `dry_run`, the merge commit sha and the sweep's `run`
id. A sample:

```json
{"artifacts":{"count":1,"paths":["uv.lock"]},"base_ref":"main","base_sha":"base0","checks":[{"app":"github-actions","conclusion":"success","name":"ci"},{"app":"github-actions","conclusion":"skipped","name":"test"}],"decision":"MERGED","dry_run":false,"event":"verdict","green_by_skip":true,"head_sha":"h7","merge_commit_sha":"9f2c1e7d4b3a0000000000000000000000000000","pr":7,"reason":"lockfile-only; green: ci, test (green by skip: test)","repo":"o/r","run":"20261115T081452Z-5","shape":"lockfile-only","source":"dependabot","tier":"patch","title":"bump x from 1.0.0 to 1.0.1","ts":"2026-11-15T08:14:52Z","v":1}
```

One writer (the sweep) appends under an exclusive lock, so a single file is
safe. It is not the control plane's per-run Ledger (one committed file per
loop run): that lives in another repository and a commit per sweep is the
wrong cost for a record read back every half hour. To ingest it there, read
the JSONL with `gate_ledger.read()` and record one summary per `run` id —
for example `_ledger.record("merge-gate", "produced", "<n> merged",
targets=[...repos merged...], counts={decision: n, ...})` — keyed on the
last `run` already ingested, so each sweep lands exactly once.
`sweep(record=False)` lets a read-only consumer ask the gate without
appending.

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
pip install "merge-gate @ git+https://github.com/gr8monk3ys/merge-gate@v0.10.0"
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

The report separates *would merge* / *stale base* / *routed to review* /
*skipped (ungated repo)* / *drift (auto-merge on, no correctness gate)* /
*held by circuit breaker* / *no verdict (GitHub did not answer — retry, not a
finding)*.

| Variable | Default | Meaning |
|---|---|---|
| `GATE_OWNERS` | *(required)* | Comma-separated users/orgs. An owner that yields zero repos aborts the sweep rather than silently shrinking the fleet. |
| `DRY_RUN` | `1` | `0` to merge and label. |
| `INCLUDE_BOTS` | `1` | `0` to ignore Dependabot. Bots are roughly four fifths of a real queue; leaving them out is how a full queue reports as empty. |
| `ONLY_PUBLIC` | unset | `1` to skip private repos — for GitHub's capped private-repo Actions minutes, not for safety. |
| `SWEEP_BUDGET_SECONDS` | unset | Stop the walk after this many seconds and report what was reached. Checked while listing repos and before each PR is decided, never inside one. |
| `GATE_WORKERS` | `8` | How many GitHub reads run at once: repo listings during enumeration, and per-PR reads during the sweep. Writes are always serial. |
| `GATE_REPOS_YML` | `./repos.yml` | Path to the optional `repos.yml`. Absolute is safest — installed as a package there is no "next to the source file". |
| `MERGE_GATE_LEDGER` | `~/.local/state/fleet/merge-gate-ledger.jsonl` | The merge ledger. `off` disables it (report-only runs only). |
| `MERGE_GATE_MAX_PER_DAY` | `3` | Real gate merges per repo per rolling 24h; `repos.yml` `merge_gate_max_per_day` overrides per repo. |
| `GH_TRANSPORT` | `auto` | `gh` forces the binary, `rest` forces the REST API. `auto` uses the binary only when it can actually authenticate. |

### Exit status

| Code | `merge-gate` | `ci-watchdog` |
|---|---|---|
| `0` | The sweep ran and reached verdicts. It may still be incomplete — the report says so in its own section — but something was judged. | Nothing blocking was measured. |
| `1` | — | A required check is red at an open PR's head: every merge in that repo is blocked. |
| `3` | The budget ran out before a single PR was judged. The report prints `STOPPED AT BUDGET`, and the status is non-zero so a scheduler shows the run red instead of green over a sweep that did not happen. |
| `4` | A real run (`DRY_RUN=0`) could not write the merge ledger, and refused to start. |

### How a sweep spends its budget

Reads run in parallel (`GATE_WORKERS`); verdicts and writes run serially,
in queue order, after each PR's reads are in. The order matters: a verdict
depends on which base branches *this sweep* has already merged into, and
that is only known once every earlier PR has been decided (rule 4 above).
Per-repo facts — `allow_auto_merge`, the required checks on a base branch,
that branch's head — are read once per repo per sweep, not once per PR.
Before a real merge the base head is re-read once more, uncached, so the
"checks ran against the current base" rule is enforced at the moment of the
write and not against a value cached minutes earlier.

## The tools

| Script | Question it answers |
|---|---|
| `classify_pr.py` | What shape is each open PR, and would it merge? Pure classifier; `--validate` runs the self-tests plus any live fixtures you add. |
| `merge_gate.py` | Merge what earns it, at its judged head. Label the rest `needs-review`. |
| `ci_watchdog.py` | Is each repo's **default branch** still green — and since when? A red default branch blocks every merge in that repo and looks identical, from outside, to a repo with nothing to do. One repo sat that way for 37 days holding 15 mergeable PRs. |
| `verify-gates.py` | Does every *required* check actually fire on pull requests? A required check that only runs on push never goes green on a PR, so nothing can ever merge. (A script, not an importable module — it is not installed.) |
| `gh_transport.py` | Can this environment reach GitHub at all, and by which route? `--whoami` answers; `--difftest` proves the two routes agree. |

Optional: `repos.yml` (see `repos.example.yml`), looked for at
`GATE_REPOS_YML`, else `repos.yml` in the working directory. Per repo it
reads `data_dir` (opt-in data paths), `may_skip` (required checks that may
pass as skipped), `correctness_checks` (check names that are gates despite
their names), `merge_gate_max_per_day`, `merge_gate_paused` and
`merge_gate_ack_reverts`, `allow_tests_only`, `require_verify_evidence`,
`verify_workflow`, `allow_reviewed` and `allow_reviewed_content`.
Every absent field is the strict value. The
merge policy itself is constants at the top of `classify_pr.py`, and
changing what merges is a one-line, deliberate edit.

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

On Windows, run `pytest` from Git Bash (or with Git's `usr/bin` on `PATH`):
the transport's timeout tests shell out to `sleep`, which PowerShell and
`cmd.exe` do not have.

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
