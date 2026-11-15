# Changelog

Releases before 0.9.0 are described by their commit and PR messages;
`DECISIONS.md` tells why each rule exists.

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
