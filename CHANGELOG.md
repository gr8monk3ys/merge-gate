# Changelog

Releases before 0.9.0 are described by their commit and PR messages;
`DECISIONS.md` tells why each rule exists.

## 0.13.0 — a reader for the shaped PRs too

### Added

- **`require_diff_review`.** In a repo that sets it, a PR merging on its
  allowlisted shape also needs a `merge-gate/diff-review` commit status of
  `success` on the exact head the gate judges; otherwise it routes and the
  report says which state it read. Its own context: it never satisfies the
  reviewed path, and a PR on the reviewed path does not owe it. Read only
  once the checks are green; an unreadable status raises. The ledger row
  gains `diff_review`. It may be set once under the registry's top-level
  `defaults:`, the only gate field that can be, with a repo's own value
  winning.

## 0.12.1 — the CI-edit rule reads the range it merges

### Security

- **A PR on declared checks that edits anything under `.github/` routes.**
  0.12.0 looked for `.github/workflows/` in the PR's path list, which is read
  before the head and capped by the API. It now reads the compare of exactly
  base...head (the range `--match-head-commit` merges), counts a rename out
  of `.github/`, covers composite actions, and a truncated compare raises.

## 0.12.0 — the reviewed path reaches the PRs it missed

The first real sweep with 0.11.0 found 2 reviewable PRs among 27.

### Added

- **An owner's own PR is in the queue of a repo with `allow_reviewed`.** A
  ready PR whose author is in `GATE_OWNERS` is judged like the fleet's
  output, and merges only on a review, never on its shape. Drafts are still
  skipped; other authors are never in the queue (`in_queue`).
- **`gate_required_checks`.** Check-run names the gate requires on a base
  GitHub reports no required checks for. A private repo on a free plan
  cannot have branch protection (§33), so every PR there was drift forever.

### Fixed

- **A `tests-only` PR the shape rules decline now falls back to the reviewed
  path.** 0.11.0 decided reviewability before the tests-only opt-in and veto
  ran, so those PRs routed to a human even in an opted-in repo. A reviewed
  tests-only PR does not owe the shape's test-count evidence.

## 0.11.0 — the reviewed path

### Added

- **`allow_reviewed`: a reviewer's pass on the head can stand in for the
  shape allowlist.** A PR in the gate's queue whose shape is `mixed`,
  `content` or `empty` routed to a human, always. In a repo that sets
  `allow_reviewed: true` it now merges when its required checks are green,
  its base is fresh, no breaker holds it, and the newest `merge-gate/review`
  commit status on the judged head is `success`. `allow_reviewed_content:
  true` extends that to PRs the content veto stops. Both are literal-`true`
  opt-ins; absent, nothing changes and no status is read. An unreadable diff
  is never reviewable. The ledger row gains `review`. See README, *The
  reviewed path*, and DECISIONS §40.

## 0.10.6 — a damaged artifact is refused, not raised

### Fixed

- **`parse_verify_report` returns a reason for every artifact it cannot
  read.** A zip with the encrypted flag set, an unsupported compression
  method, a corrupt deflate stream, or JSON nested too deep raised
  `RuntimeError`, `NotImplementedError`, `zlib.error` or `RecursionError`.
  The sweep only catches `ReadFailed`, so one such artifact, uploaded by a
  PR's own CI run, stopped the sweep for every repo. Nothing merged on it.

### Added

- **Property tests for the classifier, the version rule, the tests-only
  veto and the report parser** (`tests/test_gate_props.py`); they found the
  bug above. CI's new `fuzz` job runs every property test with the `release`
  profile: 5,000 random cases per property on each PR and push to `main`.

## 0.10.5 — growth needs both measures

### Security

- **A stage's growth is the rise in `tests`, capped by its new unique IDs.**
  0.10.4 counted IDs alone, so splitting a rerun test's one ID into two
  counted as a new test.

## 0.10.4 — growth counted in IDs

### Security

- **Where both sides list `passed_ids`, a stage's growth is its new unique
  IDs**, not its rise in `tests`. Since 0.10.3 allows fewer IDs than passes,
  running an existing test twice could otherwise pass as a new test.

## 0.10.3 — de-duplicated test IDs

### Changed

- **`passed_ids` may be fewer than passed tests.** A test that passes under
  several runners (two Playwright projects, a parametrized case reported
  once) is one ID. The list must still be sorted, unique and non-empty, and
  more IDs than passed tests is still no evidence.

## 0.10.2 — no evidence downgrade

### Fixed

- **A head report without `passed_ids` no longer escapes the identity check.**
  If a stage carries `passed_ids` at base but not at head, the PR routes
  (`stage <name>: passed_ids at base but not at head`) instead of falling back
  to the count rules.

## 0.10.1 — evidence provenance and test identity

### Security

- **A verify-report counts only from the declared workflow, event and
  newest run.** New per-repo `repos.yml` field `verify_workflow` (a path,
  e.g. `.github/workflows/ci.yml`). The head's report must come from the
  newest completed `pull_request` run of that workflow on the head sha; the
  base's from the newest completed `push` run of it on the base branch. That
  run must have concluded `success` and carry exactly one valid report; an
  older run is never used in its place. Without `verify_workflow` there is no
  evidence.
- **Tests-only: `skipped` may not rise in any stage**, and stages may carry
  an optional `passed_ids` (sorted, unique, non-empty strings, one per passed
  test; additive to schema 1). When head and base both carry them, every ID
  that passed at base must pass at head; the reason names the first missing
  one. Without IDs on both sides, the count rules apply plus the skipped rule.
  Over 200,000 IDs in a report is no evidence.

### Changed

- **Opted-in repos must add `verify_workflow`** or their tests-only (and
  `require_verify_evidence`) PRs route with `VERIFY — no evidence:
  verify_workflow is not set for this repo in repos.yml`.
- Ledger `artifacts.verify` now records each side's `run`, `workflow` and
  `event`; `passed_ids` are never written to it.
- A report may be up to 32 MiB (was 1 MiB) to hold test IDs.
- README and DECISIONS §39 state the residual limit plainly: the report comes
  from CI running the PR's own code, so it guards against accidental
  weakening, not a deliberately malicious PR.

## 0.10.0 — test-count evidence

### Added

- **CI test counts gate tests-only merges.** A repo's workflow uploads an
  Actions artifact `verify-report` (one file, `verify-report.json`: schema 1,
  repo, tested commit, per-stage tests/passed/failed/skipped) for PR heads and
  default-branch pushes. `verify_report()` reads the newest completed run on
  that sha carrying it and validates it strictly; any problem is no evidence,
  with the reason. Read once per commit per sweep, only for green PRs.
- In repos with `allow_tests_only: true`, a tests-only PR now also needs
  evidence for its head and its base: no failure at head, no stage dropped,
  per-stage `tests` and `passed` not lower, and total tests up by at least the
  test cases the diff adds (at least one when they can't be counted).
  Otherwise it routes with a `VERIFY — ...` reason.
- `repos.yml` `require_verify_evidence: true`: every shape the gate merges in
  that repo needs head and base evidence with no failure at head and no
  vanished stage.
- Ledger rows carry the head and base counts in `artifacts.verify`.
- `gh_transport.download()` for binary bodies; the REST route does not send
  the token past GitHub's redirect to artifact storage.

### Changed

- An opted-in repo whose CI does not upload `verify-report` stops getting
  tests-only merges: they route to review until it does.

## 0.9.2 — tests-only is opt-in

### Changed

- **tests-only PRs route to review unless the repo sets `allow_tests_only:
  true`** in repos.yml. No scan of a new file's text can prove it doesn't stop
  the run early (`os._exit` reached through `__import__`, `importlib`,
  `setattr`, ...), so this waits for CI-reported test counts that make a drop
  visible. Opted-in repos keep the 0.9.1 rules.

## 0.9.1 — tests-only allowlist

### Fixed

- **A new file in a tests-only PR must be a test module** (`test_*.py`,
  `*_test.py`, `*.test.*`, `*.spec.*`). Helpers, `__init__.py`, fixture data,
  configs and dotfiles change what runs without being tests, so they route to
  review.
- **More harness files**: `.pytest.ini`, `pytest.toml`, `package.json`,
  `tsconfig*.json`, Babel config and `.env*` count as test-run config.
- **Early exits are vetoed**: `os._exit`, `sys.exit`, `pytest.exit`,
  `process.exit`, bare `exit()`/`quit()`, Go `TestMain`, and patching
  `sys.modules` or builtins. `os._exit(0)` at import made pytest report
  success having run nothing.

## 0.9.0 — trust hardening

### Added

- **Merge ledger** (`gate_ledger.py`). Every verdict appends one JSON line to
  `MERGE_GATE_LEDGER` (default `~/.local/state/fleet/merge-gate-ledger.jsonl`):
  repo, PR, judged head and base sha, shape, tier, each required check's
  name/conclusion/app, `green_by_skip`, changed paths, source, decision,
  reason, `dry_run`, merge commit sha, run id. A real run that cannot write it
  exits `4` without merging.
- **DRIFT** verdict: auto-merge is on but no required check is a correctness
  gate (`is_correctness_check`, same lists as the control plane's, with
  `repos.yml` `correctness_checks` as an override). Never merges.
- **Circuit breakers** — a green PR is **HELD** when the repo is paused
  (`merge_gate_paused`), when a revert of a gate merge is detected and not yet
  acknowledged (`merge_gate_ack_reverts`), when the repo could not be scanned
  for reverts this sweep, when it has had `MERGE_GATE_MAX_PER_DAY` (default 3,
  per repo `merge_gate_max_per_day`) real gate merges in the last 24h, or when
  a correctness check on the base head is red.
- `detect_reverts()`, run at the start of every sweep.
- `tests-only` now auto-merges **only brand-new test files**, judged from the
  compare API for exactly `base_sha...head_sha` (the head read once, the same
  sha passed to `--match-head-commit`). Any modified, removed, renamed or
  copied test file, any test harness/config file (conftest.py, pytest.ini,
  tox.ini, setup.cfg, pyproject.toml, jest/vitest/karma/mocha config,
  Playwright/Cypress config or e2e/playwright/cypress dirs), a new name that
  collides with an existing path, a duplicate test name, an unreadable patch
  or base tree, or a disabling marker in a new file (skip/xfail/.only/.todo
  attributes, `pytestmark`, `SkipTest`, `__test__ =`, aliased mark imports,
  xit/fit, `t.Skip`, `#[ignore]`) routes the PR to review.
- `repos.yml` fields: `may_skip`, `correctness_checks`,
  `merge_gate_max_per_day`, `merge_gate_paused`, `merge_gate_ack_reverts`.
- `sweep(record=False)` for read-only consumers.

### Changed

- Check runs are read with `per_page=100` across every page.
- Duplicate check names resolve to the newest run (completed_at, else
  started_at, then id), not whichever GitHub listed last.
- `skipped` no longer counts as green unless the check is in `may_skip`.
- A required check that has not finished is pending, not green.
- A repo with auto-merge on and no required checks is DRIFT, not SKIPPED.

### Exit status

- `4`: a real run could not write the merge ledger.
