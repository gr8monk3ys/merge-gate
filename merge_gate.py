#!/usr/bin/env python3
"""Phase 2: merge PRs that earn it — at their judged head — label the rest.

Four conditions, ALL required, no overrides:

  1. diff shape is on the allowlist and carries no content veto
  2. the repo has >= 1 required status check on the base branch
  3. every required check is currently green
  4. those checks ran against the base branch's CURRENT head

Condition 2 is the one that was missing and is why dotfiles#41 merged with
"no checks reported". A shape match on an ungated repo does NOT merge — it
routes to review like anything else.

Condition 4 is why finance-owl's main went red on 2026-09-02: the gate merged
#148 and, 21 seconds later, #145 -- green, but green against the main that
#148 had just replaced. Branch protection is `strict: false` almost
everywhere here, so GitHub never re-runs checks on a moved base; the gate
has to notice on its own, and it has to notice per merge, because its own
merges are what move the base. One merge per base branch per sweep.

Only machine-produced PRs are ever considered -- those labelled `automated`,
those on an `auto/*` head branch, and dependabot's. Work opened by a human is
never auto-merged, whatever its diff looks like.

Usage:
    GATE_OWNERS=you,your-org python3 merge_gate.py   # report only (default)
    DRY_RUN=0 python3 merge_gate.py                  # merge / label
    INCLUDE_BOTS=0 python3 merge_gate.py             # ignore dependabot PRs
    ONLY_PUBLIC=1 python3 merge_gate.py              # skip private repos

ONLY_PUBLIC exists for Actions minutes, not for safety. Public repos have
unlimited Actions; private repos share a capped ~2000 min/month, and every
merge rebases sibling dependabot PRs and re-triggers their CI. Draining
public repos first spends nothing.
"""

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_ledger as ledger  # noqa: E402
from classify_pr import (  # noqa: E402
    DEFAULT_WORKERS, Abandoned, _run_gh, auto_mergeable, bail_if, bump_kind,
    changed_paths, classify, conflict_self_heals, content_veto,
    enumerate_open_prs, is_conflict, is_correctness_check, is_dependabot,
    load_data_dirs, load_gate_config, promote_candidate, repo_visibility,
)

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
ONLY_PUBLIC = os.environ.get("ONLY_PUBLIC") == "1"
NEEDS_REVIEW = "needs-review"
# Exit status when the budget ran out before a single PR was judged. Distinct
# from 0 (a sweep that reached verdicts, complete or not) and from 1 (what
# ci_watchdog uses for a measured blocker), so a scheduler can tell "nothing
# happened" from "something was found". The 2026-10-06 22:17 run printed
# STOPPED AT BUDGET with 109 PRs unreached and exited 0, and the task showed
# green: a sweep that judged nothing and said so quietly is a sweep nobody
# notices did not happen.
EXIT_NO_VERDICTS = 3
# Exit status when a real run (DRY_RUN=0) cannot write its merge ledger. The
# rate and revert breakers are computed from that file; a gate that cannot
# record its merges cannot count them, so it refuses to start.
EXIT_NO_LEDGER = 4
# How many changed paths a ledger row carries. The count is always exact.
LEDGER_MAX_PATHS = 50
# Prose for the report, but compared by code: this token links check_state's
# conflict verdict to the self-heal in _evaluate. One definition, or a wording
# tweak silently disables the rebase kick with no failure signal.
CONFLICTING = "CONFLICTING with base"
# Same contract: a prefix _evaluate matches on to decide who gets the rebase
# kick. Reworded here, silently nobody is asked to rebase.
STALE_BASE = "STALE BASE"
# Same contract again: the prefix _decide files under `drift` rather than
# `skipped`. A repo that permits auto-merge but has no correctness gate is
# not "deliberately ungated" -- it is a Phase 1 setting that no longer
# matches the repo, and it needs a person to fix the setting or the gate.
DRIFT = "DRIFT"
# Prefix of every circuit-breaker hold. A held PR is green and allowlisted;
# something about the REPO says not now (see breaker_reason).
HELD = "HELD"
# Gate merges allowed per repo per rolling 24h, unless MERGE_GATE_MAX_PER_DAY
# or the repo's merge_gate_max_per_day says otherwise.
DEFAULT_MAX_PER_DAY = 3
# How far back revert detection looks for gate merges to protect.
REVERT_LOOKBACK_DAYS = 14
# Base-branch conclusions that mean "main is broken". `cancelled` is not one:
# concurrency groups cancel superseded runs on main all day, and the newest
# run of the same name is what is read anyway.
BASE_RED = {"failure", "timed_out", "action_required", "startup_failure"}
_RE_REVERTS_SHA = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
_RE_REVERTS_PR = re.compile(r"\bReverts ([\w.-]+/[\w.-]+)#(\d+)")
REBASE_NAG_HOURS = 24  # at most one @dependabot rebase request per PR per day


class ReadFailed(Exception):
    """A gh read did not answer. This is NOT a fact about the repository.

    Every skip reason this script prints is a claim: "auto-merge disabled (no
    real test gate)" asserts a Phase 1 decision, "no required checks on main"
    asserts the repo is ungated. Both were being printed whenever a TLS
    handshake blipped, because sh() returned a nonzero code and each caller
    read that as the negative answer. Successive runs disagreed with each
    other -- armed went 9 -> 6 -> 5 in ten minutes while nothing changed on
    GitHub, and trading-bot#70 was reported as living on an unprotected branch
    one run after the gate listed its five green required checks.

    Failing closed made none of that unsafe. It made the report untrustworthy,
    which is worse than useless: a reader cannot tell a repo that needs gating
    from a socket that needed retrying.
    """


def sh(*args):
    """Best-effort gh call. Returns (code, stdout, stderr); never raises.

    Retries transient network/rate failures -- the fleet sweep issues a few
    thousand calls and a burst of `tls: failed to verify certificate` once
    half-applied it.
    """
    r = _run_gh(["gh", *args])
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def sh_strict(*args):
    """gh call that distinguishes "GitHub said no" from "GitHub did not say".

    Returns stdout on success and None when GitHub genuinely answered 404 --
    an unprotected branch really does 404, and that is a fact worth acting on.
    A 403 that names the plan is the same fact: a private repo on a free plan
    cannot have branch protection, so GitHub has answered and the answer will
    not change on retry. Anything else raises, because the absence of an
    answer must never be rendered as one.
    """
    code, out, err = sh(*args)
    if code == 0:
        return out
    low = err.lower()
    if ("404" in low or "not found" in low or "not protected" in low
            or "upgrade to github pro" in low):
        return None
    raise ReadFailed(err.splitlines()[0][:120] if err else
                     f"gh {' '.join(args)} exited {code}")


def is_loop_produced(repo, number, pr):
    """Is this PR the fleet's own output?

    The `automated` label alone is NOT sufficient: it is applied inconsistently
    and misses roughly 44% of loop output (seo-wordbubble#13, produced by
    finish_scaffold on branch auto/finish-20260717, carries no labels at all).

    The deterministic `auto/<loop>-<date>` head branch mandated by README.md is
    the reliable marker, so accept either. Human-opened PRs match neither and
    are never auto-merged.
    """
    if "automated" in [l["name"] for l in pr.get("labels", [])]:
        return True

    # Dependabot is machine output too, and it was 209 of the 268 actionable
    # open PRs -- four fifths of the queue this gate exists to drain -- yet
    # invisible to it, because bot PRs carry neither the `automated` label nor
    # an auto/* branch. It was opt-in (INCLUDE_BOTS=1) for a while, and every
    # run that forgot the flag reported a drained queue. It is now default;
    # INCLUDE_BOTS=0 opts out. Nothing is relaxed: bots still need an
    # allowlisted shape and a green required check, so a bump touching
    # package.json alongside its lockfile is `mixed` and still routes to a
    # human, exactly as album-conceptualizer#62 does today.
    if os.environ.get("INCLUDE_BOTS", "1") != "0" and is_dependabot(pr):
        return True

    # `gh pr list` already told us the head ref; only pay for an API call when
    # it did not. fetch_open_prs() requests headRefName, so this is the
    # fallback path for callers passing a leaner PR dict.
    head = pr.get("headRefName")
    if head is None:
        code, out, _ = sh("api", f"repos/{repo}/pulls/{number}", "--jq", ".head.ref")
        head = out if code == 0 else ""
    return head.startswith("auto/")


def required_checks(repo, branch):
    # Parse the JSON array. Do NOT join/split on "," — check names legitimately
    # contain commas ("Lint, Test & Build"), and the round-trip shreds them into
    # phantom contexts that can never be satisfied.
    out = sh_strict("api", f"repos/{repo}/branches/{branch}/protection"
                           "/required_status_checks", "--jq", ".contexts")
    # None is GitHub's 404 for "this branch has no protection" -- a real
    # answer. An unparseable body is not; refusing to guess keeps an ungated
    # repo distinguishable from an unread one.
    if out is None or not out:
        return []
    try:
        return json.loads(out) or []
    except json.JSONDecodeError:
        raise ReadFailed(f"{repo}: unparseable required_status_checks")


def auto_merge_allowed(repo):
    """Does the repo permit auto-merge at all?

    Phase 1 turns this OFF wherever no genuine test gate exists. Respecting it
    keeps that judgement authoritative: fraud-stream has a required
    `security-baseline` check, but a security scan is not a correctness gate,
    so it must not arm merely because something green exists.
    """
    out = sh_strict("api", f"repos/{repo}", "--jq", ".allow_auto_merge")
    if out is None:
        raise ReadFailed(f"{repo}: repository not found")
    return out.strip() == "true"


def check_runs(repo, sha):
    """Every check run on `sha`, across every page. Raises ReadFailed.

    The check-runs endpoint pages at 30 by default. It used to be read as one
    page, so a commit with more than 30 runs -- a matrix build is enough --
    could have its required check on page two, read as "not run", or worse,
    have only an older attempt of it visible. `--paginate` with a
    per-page projection emits one JSON array per page, one per line (§23).
    """
    out = sh_strict("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                    "--paginate", "--jq",
                    "[.check_runs[]|{id,name,status,conclusion,started_at,"
                    "completed_at,app:.app.slug}]")
    if out is None:
        raise ReadFailed(f"{repo}: check-runs not found for {sha[:8]}")
    runs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            page = json.loads(line)
        except json.JSONDecodeError:
            raise ReadFailed(f"{repo}: unparseable check-runs for {sha[:8]}")
        if not isinstance(page, list):
            raise ReadFailed(f"{repo}: unexpected check-runs shape for {sha[:8]}")
        runs.extend(r for r in page if isinstance(r, dict))
    return runs


def _run_age(run):
    """Sort key: newer runs sort higher. completed_at, else started_at (an
    in-flight re-run has no completed_at yet), then id as the tiebreak --
    ids are allocated in creation order."""
    return (run.get("completed_at") or run.get("started_at") or "",
            run.get("id") or 0)


def latest_by_name(runs):
    """{name: newest run}. A re-run creates a second run with the same name;
    keeping whichever the API listed last let a stale success shadow a newer
    failure, or the reverse."""
    out = {}
    for r in runs:
        name = r.get("name")
        if name is None:
            continue
        if name not in out or _run_age(r) > _run_age(out[name]):
            out[name] = r
    return out


def base_head(repo, branch):
    out = sh_strict("api", f"repos/{repo}/branches/{branch}", "--jq", ".commit.sha")
    if out is None:
        raise ReadFailed(f"{repo}: branch {branch} not found")
    return out.strip()


class RepoCache:
    """Per-repo facts, read once per sweep and shared across its threads.

    `allow_auto_merge`, the required checks on a base branch and that
    branch's head are facts about the REPO, and were being re-read for every
    PR in it: a repo with twenty Dependabot PRs cost sixty reads for three
    answers. One read per key per sweep, and a key being read by one thread
    makes the others wait for it rather than issue the same call eight times.

    A failed read is NOT cached. Caching the failure would turn one blip into
    a repo's worth of NO VERDICT rows; the next PR retries instead, exactly
    as before.
    """

    def __init__(self, gate_cfg=None):
        self._vals, self._locks = {}, {}
        self._lock = threading.Lock()
        # repos.yml's per-repo gate settings (load_gate_config), keyed by bare
        # repo name. Configuration, not a read: it is fixed for the sweep.
        self.gate_cfg = gate_cfg or {}

    def config(self, repo):
        return self.gate_cfg.get(repo.split("/")[-1]) or {}

    def get(self, key, read):
        with self._lock:
            if key in self._vals:
                return self._vals[key]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._lock:
                if key in self._vals:
                    return self._vals[key]
            val = read()
            with self._lock:
                self._vals[key] = val
            return val


def stale_base_reason(repo, base_ref, base_sha, moved=frozenset(), head=None):
    """Why this PR's green checks describe a base that no longer exists, or None.

    `pulls/{n}.base.sha` is the base commit GitHub recorded when the head was
    last pushed -- the base its checks ran against. It is NOT refreshed when
    the base branch moves, and with `strict: false` protection (57 of 58
    repos here) `mergeable_state` stays `clean` rather than going `behind`:
    trading-bot#107 read `clean` with base.sha two commits behind main on
    2026-09-01. So the branch head is read directly and compared.

    `moved` is the set of (repo, base_ref) this sweep has already merged
    into. Every remaining candidate on such a base is stale by construction,
    and saying so from memory rather than from a ref read means the verdict
    cannot lose a race with the merge that caused it. `head` is the branch
    head if the caller already read it; otherwise it is read here.
    """
    if (repo, base_ref) in moved:
        return f"{STALE_BASE} — {base_ref} moved earlier this sweep"
    if head is None:
        head = base_head(repo, base_ref)
    if head != base_sha:
        return (f"{STALE_BASE} — checks ran against {base_sha[:8]}, "
                f"{base_ref} is at {head[:8]}")
    return None


class State:
    """What GitHub says about one PR's mergeability, before any verdict.

    Everything here is a READ. The verdict -- which also depends on what this
    sweep has already merged -- is `judge()`, a pure function of this object
    and the `moved` set, so reads can run in parallel and verdicts in order.
    Fields past the first short-circuit are None: a draft's required checks
    are never fetched, exactly as before.
    """

    def __init__(self, repo, number, sha, base, base_sha, draft, mstate,
                 cfg=None):
        self.repo, self.number = repo, number
        self.sha, self.base, self.base_sha = sha, base, base_sha
        self.draft = draft.strip().lower() == "true"
        self.conflict = is_conflict(mstate)
        cfg = self.cfg = cfg or {}
        self.may_skip = list(cfg.get("may_skip") or [])
        self.declared = list(cfg.get("correctness_checks") or [])
        self.allow_auto: bool | None = None       # repo permits auto-merge
        self.required: list | None = None         # required contexts on base
        self.missing: list | None = None          # required, no check-run
        self.failed: list | None = None           # required, run is red
        self.pending: list | None = None          # required, not finished
        self.skipped: list | None = None          # required, skipped, not allowed
        self.green_by_skip = False                # a may_skip check passed as skipped
        self.checks: list = []                    # [{name, conclusion, app}] per required
        self.branch_head: str | None = None       # where base was when read
        self.base_red: list = []                  # correctness checks red on base

    @property
    def correctness(self):
        """The required checks that are gates on the code still working."""
        return [c for c in self.required or []
                if is_correctness_check(c, self.declared)]


def grade_checks(st, latest):
    """Sort each required check into exactly one bucket on `st`.

    Three ways a required check used to count as green that it was not:

      * `skipped` passed unconditionally. A path-filtered job that did not
        run verified nothing, and a required test job that skips on a
        tests-only diff is the very job that should have run. It now passes
        only when the repo lists it in `may_skip`, and the PR then records
        `green_by_skip` so the ledger shows which merges rode on one.
      * a run still in progress (conclusion null) was neither missing nor
        failed, so it fell through as green.
      * duplicate names resolved last-listed-wins; `latest` is newest-wins.
    """
    st.missing, st.failed, st.pending, st.skipped = [], [], [], []
    st.green_by_skip, st.checks = False, []
    for c in st.required:
        run = latest.get(c)
        if run is None:
            st.missing.append(c)
            st.checks.append({"name": c, "conclusion": "missing", "app": None})
            continue
        concl = run.get("conclusion")
        st.checks.append({"name": c, "conclusion": concl or run.get("status")
                          or "pending", "app": run.get("app")})
        if concl is None:
            st.pending.append(c)
        elif concl == "success":
            pass
        elif concl == "skipped":
            if c in st.may_skip:
                st.green_by_skip = True
            else:
                st.skipped.append(c)
        else:
            st.failed.append(c)


def read_state(repo, number, cache=None, stop=None, cfg=None):
    """Read everything judge() needs about one PR. Raises ReadFailed.

    Per-repo facts go through `cache`; per-PR facts are read every time.
    Short-circuits in the same order as the old check_state(), so a draft or
    an ungated repo still costs one call, not five.
    """
    cache = cache if cache is not None else RepoCache()
    cfg = cfg if cfg is not None else cache.config(repo)
    out = sh_strict("api", f"repos/{repo}/pulls/{number}",
                    "--jq", ".head.sha,.base.ref,.draft,.mergeable_state,.base.sha")
    if out is None:
        raise ReadFailed(f"{repo}#{number}: pull request not found")
    lines = out.splitlines()
    if len(lines) < 5:
        raise ReadFailed(f"{repo}#{number}: short response from pulls endpoint")
    sha, base, draft, mstate, base_sha = lines[:5]
    st = State(repo, number, sha, base, base_sha, draft, mstate, cfg)

    # A draft can never merge and can never be armed, however green it is.
    # 22 of 56 open PRs sat in this state while pr-shepherd reported them as
    # "awaiting-review" — they were never eligible for review to matter.
    if st.draft:
        return st

    bail_if(stop)
    st.allow_auto = cache.get(("allow_auto_merge", repo),
                              lambda: auto_merge_allowed(repo))
    if not st.allow_auto:
        return st

    bail_if(stop)
    st.required = cache.get(("required_checks", repo, base),
                            lambda: required_checks(repo, base))
    if not st.required or not st.correctness:
        return st

    # An armed PR that goes CONFLICTING sits forever: auto-merge can never
    # fire on a dirty PR, however green its checks. finance-owl#69–79 sat
    # exactly there on 2026-08-15 — five armed bumps, required checks green,
    # all dirty over the shared lockfile — until rebases were requested by
    # hand. (Why only "dirty" counts: see is_conflict.)
    if st.conflict:
        return st

    bail_if(stop)
    # An empty list here is a real answer: no check has reported yet. That
    # lands in judge() as "required check(s) not run", which blocks.
    grade_checks(st, latest_by_name(check_runs(repo, sha)))
    if st.missing or st.failed or st.pending or st.skipped:
        return st

    # Green -- against which base? Read only for a green PR, so a red one
    # still costs what it used to; cached, so a repo's base is read once
    # however many green PRs sit on it.
    bail_if(stop)
    st.branch_head = cache.get(("base_head", repo, base),
                               lambda: base_head(repo, base))

    # Is the base itself broken? Merging onto a red main stacks a change on
    # a failure nobody has diagnosed, and a green PR proves nothing there:
    # its checks ran against a base that already did not work. Every
    # correctness check on the base head counts, required or not. Cached per
    # base head, so a repo's base is read once however many PRs sit on it.
    bail_if(stop)
    runs = cache.get(("base_runs", repo, st.branch_head),
                     lambda: check_runs(repo, st.branch_head))
    st.base_red = sorted(
        name for name, run in latest_by_name(runs).items()
        if run.get("conclusion") in BASE_RED
        and is_correctness_check(name, st.declared))
    return st


def judge(st, moved=frozenset()):
    """(verdict, summary) for a State: may this PR merge now?

    None = never eligible as-is (draft, ungated repo); False = not now
    (required checks red or missing, conflicting with base, or green against
    a base that has since moved); True = green and fresh. Pure: the only
    input besides the reads is `moved`, the bases this sweep has merged
    into, which is why it runs serially after every read is in.
    """
    if st.draft:
        return None, "DRAFT — cannot merge until marked ready"
    if not st.allow_auto:
        return None, "repo has auto-merge disabled (no real test gate)"
    # allow_auto_merge is Phase 1's assertion that a real test gate exists.
    # When the required checks do not bear that out, the assertion has
    # drifted: fraud-stream's lone required `security-baseline` is green on
    # code that does not build. Not a merge, and not "deliberately ungated".
    if not st.required:
        return None, (f"{DRIFT} — allow_auto_merge is on but {st.base} has "
                      "no required checks")
    if not st.correctness:
        return None, (f"{DRIFT} — allow_auto_merge is on but no required check "
                      f"on {st.base} is a correctness gate "
                      f"(required: {', '.join(st.required)}; declare real gates "
                      "in repos.yml correctness_checks)")
    if st.conflict:
        return False, CONFLICTING
    if st.missing:
        return False, f"required check(s) not run: {', '.join(st.missing)}"
    if st.failed:
        return False, f"required check(s) red: {', '.join(st.failed)}"
    if st.skipped:
        return False, ("required check(s) skipped, and not in this repo's "
                       f"may_skip: {', '.join(st.skipped)}")
    if st.pending:
        return False, f"required check(s) still running: {', '.join(st.pending)}"
    # Checked last so a red PR is still reported as red: that is the finding
    # a reader can act on, and a rebase would only re-run the same red checks.
    stale = stale_base_reason(st.repo, st.base, st.base_sha, moved,
                              head=st.branch_head)
    if stale:
        return False, stale
    skipped = [c["name"] for c in st.checks if c["conclusion"] == "skipped"]
    return True, (f"green: {', '.join(st.required)}"
                  + (f" (green by skip: {', '.join(skipped)})" if skipped else ""))


def check_state(repo, number, moved=frozenset(), cache=None):
    """Return (verdict, summary, head_sha, base_ref): may this PR merge now?

    read_state() then judge(), for callers that want the answer in one call.
    head_sha is the exact commit the verdict describes — the caller must
    merge THAT commit or nothing, which is what closes the re-arm race.
    base_ref is what the caller records in `moved` after it merges, so the
    next candidate on the same base is judged stale without a ref read.
    """
    st = read_state(repo, number, cache)
    green, detail = judge(st, moved)
    return green, detail, st.sha, st.base


def request_rebase(repo, num):
    """Ask dependabot to rebase a conflicted PR, throttled per REBASE_NAG_HOURS.

    Dependabot rebases its own PRs on request via a magic comment. The
    throttle matters because this gate runs on a 30-minute loop and a PR
    dependabot *cannot* rebase (it replies saying so) would otherwise be
    nagged 48 times a day. The PR's own comment timeline is the throttle
    state — the only store every runner of this script shares, and the
    comment IS the action, so the record cannot drift from reality.
    Returns a suffix for the report line.
    """
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=REBASE_NAG_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, out, _ = sh("api", f"repos/{repo}/issues/{num}/comments?since={since}",
                      "--jq", 'any(.[]; .body|test("@dependabot (rebase|recreate)"))')
    if code != 0:
        # Best-effort by design: a blipped read must not un-decide a PR whose
        # verdict already exists, and posting blind risks the double-nag the
        # throttle exists to prevent. Skip; the next sweep retries.
        return " — rebase skipped: could not read comments"
    if out.strip() == "true":
        return " — rebase already requested"
    if DRY_RUN:
        return " — would request dependabot rebase"
    code, _, err = sh("pr", "comment", str(num), "--repo", repo,
                      "--body", "@dependabot rebase")
    return (" — rebase requested" if code == 0
            else f" — rebase request failed: {err[:40]}")


class Sweep:
    """One run's verdicts, and the one piece of state that carries between
    them: which base branches this sweep has already moved.

    `moved` is filled in DRY_RUN too. The report must show what a real run
    would do, and a real run merges one PR per base and stales the rest; a
    dry run that listed four WOULD MERGE rows for one repo would be
    describing the sweep that put finance-owl's main in the red.
    """

    def __init__(self, record=True):
        self.armed, self.review, self.skipped = [], [], []
        self.stale, self.failed = [], []
        self.drift = []      # auto-merge on, no correctness gate: see DRIFT
        self.held = []       # green, but a circuit breaker said not now
        # The merge ledger as read at the start of the sweep, plus any pause
        # rows this sweep's revert scan added. Read lazily for callers that
        # judge single PRs. This sweep's own merges are counted from `armed`.
        self.history = None
        # Repos whose revert scan GitHub did not answer: merges there are
        # held, because "could not check for a revert" is not "no revert".
        self.unscanned = set()
        # Whether verdicts are appended to the merge ledger. sweep(record=False)
        # lets a read-only consumer (a dashboard) ask without writing.
        self.record = record
        self.run_id = (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                       + f"-{os.getpid()}")
        self.moved = set()   # {(repo, base_ref)}
        # Set when a Budget stopped the walk early. `unreached` is what was
        # left, so a caller can say how much of the queue it never looked at
        # rather than implying it looked at all of it.
        self.stopped_early = False
        self.unreached = 0
        # How the enumeration itself went. A sweep that never finished
        # listing the fleet has an `unreached` of PRs it never even counted,
        # so the repos are reported too: "109 PRs unreached" and "73 of 110
        # repos never listed" are different findings with different fixes.
        self.repos_total = 0
        self.repos_unreached = 0
        self.repos_unlisted = []
        # Ledger appends that failed. Loud in the report; see _record().
        self.ledger_errors = []

    @property
    def verdicts(self):
        """Rows that state a decision. `failed` is not one: it says GitHub
        did not answer, which is the absence of a verdict, not a verdict."""
        return (len(self.armed) + len(self.stale) + len(self.review)
                + len(self.skipped) + len(self.drift) + len(self.held))

    def rows(self):
        if self.history is None:
            self.history = ledger.read()[0]
        return self.history


class Reading:
    """Everything the read phase learned about one PR. No verdict, no write.

    The sweep is two phases now. Reads -- the diff's paths, whether the PR
    is armed, and (for an allowlisted shape) its State -- run `workers` at a
    time, because they are independent of each other and of what the sweep
    decides. Writes and verdicts run serially, in queue order, in _decide():
    a verdict depends on `Sweep.moved`, and `moved` is only known once every
    earlier PR has been decided. Nothing in here consults `moved`.
    """

    def __init__(self, pr, repo, num):
        self.pr, self.repo, self.num = pr, repo, num
        self.title = pr.get("title", "")
        self.shape: str | None = None
        self.mergeable = False             # allowlisted shape, no content veto
        self.why: str | None = None        # review reason, when not mergeable
        self.armed = False                 # GitHub has an arm on this PR
        self.state: State | None = None    # read when mergeable
        self.paths: list | None = None     # changed paths; None = unreadable
        self.source = pr_source(pr)        # dependabot | loop | other
        # The PR's own bump severity (patch/minor/...), when it declares one.
        # Recorded as the ledger's `tier`: how far the code moved.
        self.tier: str | None = None
        self.head_sha: str | None = None   # head when the arm was read
        self.base_ref: str | None = None
        self.base_sha: str | None = None


def _read(pr, repo, num, data_dirs, cache=None, stop=None):
    """The read half of one PR's judgement. Raises ReadFailed.

    Safe to run concurrently with other PRs' reads: it touches nothing on
    the Sweep and writes nothing to GitHub. `stop` lets a read running ahead
    of the budget give up between calls once the budget is gone.
    """
    cache = cache if cache is not None else RepoCache()
    r = Reading(pr, repo, num)
    bail_if(stop)
    paths = r.paths = changed_paths(repo, num)
    title, body = r.title, pr.get("body", "") or ""
    r.tier = bump_kind(title, body)
    # classify() reads paths only; the version judgement needs the PR text.
    r.shape = promote_candidate(
        classify(paths, data_dirs.get(repo.split("/")[1])), title, body)
    r.mergeable = auto_mergeable(r.shape, paths)

    if not r.mergeable:
        if r.shape == "unreadable":
            # Distinguish "I could not read this diff" from a real veto.
            # Reporting an API failure as "content veto" sends someone
            # looking for a blog post that was never there.
            r.why = "UNREADABLE — could not fetch diff (repo gone or API error)"
        elif content_veto(paths):
            r.why = "content veto"
        else:
            # Say WHICH version rule declined it. "shape=mixed" on a
            # dependency bump sends the reader looking at file paths when
            # the real answer is a major version buried in a group.
            kind = bump_kind(title, body)
            r.why = f"shape={r.shape}" + (f"; bump={kind}" if kind else "")

    # Is there a standing arm? Read for every shape: an arm outside the
    # allowlist is drift to correct, and an arm on an allowlisted PR is a
    # standing permission to retire (see _decide).
    bail_if(stop)
    # The same read carries the head and base the ledger records for PRs that
    # never reach read_state(): every row names the commit it was about.
    out = sh_strict("api", f"repos/{repo}/pulls/{num}",
                    "--jq", ".auto_merge != null,.head.sha,.base.ref,.base.sha")
    lines = (out or "").splitlines()
    r.armed = bool(lines) and lines[0].strip() == "true"
    if len(lines) >= 4:
        r.head_sha, r.base_ref, r.base_sha = (x.strip() for x in lines[1:4])

    if r.mergeable:
        r.state = read_state(repo, num, cache, stop)
    return r


def max_per_day(cfg):
    """The repo's rolling-24h gate-merge cap: repos.yml, else env, else 3.
    An unreadable value falls back to the default, never to "unlimited"."""
    for raw in ((cfg or {}).get("merge_gate_max_per_day"),
                os.environ.get("MERGE_GATE_MAX_PER_DAY", "").strip() or None):
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_PER_DAY
    return DEFAULT_MAX_PER_DAY


def _sha_match(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def active_pauses(rows, repo, acked=()):
    """Revert pauses for `repo` that no human has acknowledged. A pause
    clears only when its revert sha is listed in merge_gate_ack_reverts:
    a later sweep finding nothing new does not clear it, and neither does
    time."""
    return [p for p in ledger.pauses(rows, repo)
            if not any(_sha_match(p.get("revert_sha"), a) for a in acked or ())]


def breaker_reason(st, s, now=None):
    """Why a green, allowlisted PR must still not merge now, or None.

    Every reason starts with HELD. Checked in order of how much a human
    must do: a pause needs repos.yml edited, a rate hold clears with time,
    a red base clears when the base is fixed. Applied in DRY_RUN too, so a
    report describes the run a real one would perform.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    repo, cfg = st.repo, st.cfg or {}
    if cfg.get("merge_gate_paused"):
        return f"{HELD} — PAUSED by repos.yml (merge_gate_paused: true)"
    rows = s.rows()
    paused = active_pauses(rows, repo, cfg.get("merge_gate_ack_reverts"))
    if paused:
        p = paused[-1]
        return (f"{HELD} — PAUSED: {p.get('revert_sha', '?')[:12]} on "
                f"{p.get('base_ref') or st.base} reverts gate merge "
                f"{(p.get('reverted_sha') or '?')[:12]} (#{p.get('pr')}); add "
                f"{p.get('revert_sha', '?')[:12]} to merge_gate_ack_reverts in "
                "repos.yml to resume")
    if repo in s.unscanned:
        return (f"{HELD} — could not scan {st.base} for reverts of gate merges "
                "this sweep (GitHub did not answer)")
    limit = max_per_day(cfg)
    since = now - dt.timedelta(hours=24)
    done = (len(ledger.real_merges(rows, repo, since=since))
            + sum(1 for row in s.armed if row[0] == repo))
    if done >= limit:
        return (f"{HELD} — merge rate: {done} gate merge(s) in {repo} in the "
                f"last 24h, limit {limit}")
    if st.base_red:
        return (f"{HELD} — {st.base} is red at {(st.branch_head or '?')[:8]}: "
                f"{', '.join(st.base_red)}")
    if s.ledger_errors and not DRY_RUN:
        return f"{HELD} — the merge ledger stopped accepting writes this sweep"
    return None


def _parse_pages(out):
    """Concatenated JSON documents (gh --paginate output) as a list of them."""
    dec, i, docs = json.JSONDecoder(), 0, []
    out = out or ""
    while True:
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            return docs
        doc, i = dec.raw_decode(out, i)
        docs.append(doc)


def branch_commits(repo, branch, since):
    """[(sha, message)] on `branch` since an ISO time. Raises ReadFailed."""
    out = sh_strict("api", f"repos/{repo}/commits?sha={branch}&since={since}"
                           "&per_page=100", "--paginate")
    if out is None:
        raise ReadFailed(f"{repo}: branch {branch} not found")
    try:
        pages = _parse_pages(out)
    except json.JSONDecodeError:
        raise ReadFailed(f"{repo}: unparseable commit list for {branch}")
    commits = []
    for page in pages:
        for c in page if isinstance(page, list) else []:
            commits.append((c.get("sha") or "",
                            ((c.get("commit") or {}).get("message") or "")))
    return commits


def detect_reverts(rows, now=None, read=branch_commits):
    """Scan each base branch the gate merged into for reverts of its merges.

    Returns (new pause rows, repos that could not be scanned). A revert is
    recognised by git's own "This reverts commit <sha>" naming a gate merge
    (its merge commit or judged head), or by GitHub's revert-PR body
    "Reverts owner/repo#N" naming a gate-merged PR. A revert already
    recorded is not recorded again. Only real merges are protected; a
    dry run merged nothing. The sweep calls this before deciding anything.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=REVERT_LOOKBACK_DAYS)
    merges = ledger.real_merges(rows, since=since)
    seen = {(p.get("repo"), p.get("revert_sha")) for p in ledger.pauses(rows)}
    targets = {}
    for m in merges:
        if m.get("repo") and m.get("base_ref"):
            targets.setdefault((m["repo"], m["base_ref"]), []).append(m)
    found, unscanned = [], set()
    for (repo, base_ref), ms in sorted(targets.items()):
        first = min(m.get("ts") or ledger.now_iso(since) for m in ms)
        try:
            commits = read(repo, base_ref, first)
        except ReadFailed:
            unscanned.add(repo)
            continue
        for sha, msg in commits:
            if (repo, sha) in seen:
                continue
            shas = _RE_REVERTS_SHA.findall(msg)
            prs = {int(n) for r, n in _RE_REVERTS_PR.findall(msg)
                   if r.lower() == repo.lower()}
            hit = next((m for m in ms
                        if m.get("pr") in prs
                        or any(_sha_match(s_, m.get("merge_commit_sha"))
                               or _sha_match(s_, m.get("head_sha")) for s_ in shas)),
                       None)
            if hit is None:
                continue
            seen.add((repo, sha))
            found.append({
                "event": "pause", "repo": repo, "base_ref": base_ref,
                "revert_sha": sha, "pr": hit.get("pr"),
                "reverted_sha": hit.get("merge_commit_sha") or hit.get("head_sha"),
                "reason": (f"{sha[:12]} on {base_ref} reverts gate merge of "
                           f"#{hit.get('pr')}"),
            })
    return found, unscanned


def pr_source(pr):
    """Who produced this PR: dependabot | loop | other. For the ledger;
    the gate's own eligibility rule is is_loop_produced()."""
    if is_dependabot(pr):
        return "dependabot"
    labels = [l.get("name") for l in pr.get("labels") or []]
    if "automated" in labels or (pr.get("headRefName") or "").startswith("auto/"):
        return "loop"
    return "other"


def ledger_row(r, decision, reason, merge_commit_sha=None):
    """The one structured record of a verdict. Pure."""
    st = getattr(r, "state", None)
    paths = getattr(r, "paths", None)
    pr = getattr(r, "pr", {}) or {}
    return {
        "event": "verdict",
        "repo": r.repo, "pr": r.num, "title": getattr(r, "title", ""),
        "source": getattr(r, "source", None) or pr_source(pr),
        "shape": getattr(r, "shape", None),
        "tier": getattr(r, "tier", None),
        # What the PR is about: its judged head, and the base its checks saw.
        "head_sha": st.sha if st else getattr(r, "head_sha", None),
        "base_ref": st.base if st else getattr(r, "base_ref", None),
        "base_sha": st.base_sha if st else getattr(r, "base_sha", None),
        "checks": list(st.checks) if st else [],
        "green_by_skip": bool(st.green_by_skip) if st else False,
        "artifacts": {"count": None if paths is None else len(paths),
                      "paths": (paths or [])[:LEDGER_MAX_PATHS]},
        "decision": decision, "reason": reason,
        "dry_run": DRY_RUN,
        "merge_commit_sha": merge_commit_sha,
    }


def _record(s, row):
    """Append a row; a failure is kept on the Sweep and reported, never
    swallowed. The verdict itself has already been reached and stands."""
    if not s.record:
        return
    try:
        ledger.append({**row, "run": s.run_id})
    except ledger.LedgerError as exc:
        s.ledger_errors.append(str(exc))


def _file(s, bucket, r, detail, decision, merge_commit_sha=None):
    """File one verdict row on the Sweep AND in the ledger. Every verdict
    goes through here, so the two cannot disagree about what was decided."""
    getattr(s, bucket).append((r.repo, r.num, detail, r.title))
    _record(s, ledger_row(r, decision, detail, merge_commit_sha))


def _merge_commit_sha(repo, num):
    """The commit a merge put on the base, or None. Best-effort: the merge
    has already happened, so a blipped read must not un-decide it. Revert
    detection also matches the judged head and the PR number."""
    code, out, _ = sh("api", f"repos/{repo}/pulls/{num}", "--jq", ".merge_commit_sha")
    out = (out or "").strip()
    return out if code == 0 and out and out != "null" else None


def _disarm(repo, num):
    if not DRY_RUN:
        sh("pr", "merge", str(num), "--repo", repo, "--disable-auto")


def _decide(r, s):
    """The verdict-and-write half: file `r` under s.armed / review / skipped
    / stale, and perform the writes that follow. Serial, in queue order.

    This is the only place `s.moved` is consulted and the only place GitHub
    is written to. Raises ReadFailed from the one read it performs itself --
    the base re-check before a real merge -- so main() can file the PR as
    unread rather than decided.
    """
    repo, num, title, pr = r.repo, r.num, r.title, r.pr

    if not r.mergeable:
        why = r.why
        # Drift correction. Declining to arm is not enough: anything already
        # armed outside the allowlist will still land on its own. pr-shepherd
        # used to arm by eye and re-armed one private repo's #4 (mixed, 10
        # files) within two days of it being disarmed by hand.
        if r.armed:
            why += " — DISARMED (was armed outside the allowlist)"
            _disarm(repo, num)
        _file(s, "review", r, why, ledger.ROUTED)
        return

    # Standing arms are retired, allowlisted shape or not. An arm outlives
    # the judgement that granted it: finance-owl#72 was armed as nodemailer
    # 8.0.2→8.0.5 (minor), dependabot force-pushed 9.0.1 — a major — into
    # the same PR at 02:21:49, and auto-merge landed it at 02:23:59. 130
    # seconds from rewrite to merge; no loop cadence polices that window.
    # The gate now merges exactly the commit it judged, or nothing.
    if r.armed:
        _disarm(repo, num)

    st = r.state
    green, detail = judge(st, s.moved)
    sha, base = st.sha, st.base

    if green is None:
        if detail.startswith(DRIFT):
            _file(s, "drift", r, detail, ledger.DRIFT)
        else:
            _file(s, "skipped", r, detail, ledger.SKIPPED)
        return

    if green:
        held = breaker_reason(st, s)
        if held:
            _file(s, "held", r, held, ledger.HELD)
            return

    if green and not DRY_RUN:
        # The base head in `st` was read once per repo, possibly minutes ago
        # and before other PRs in this very sweep were read. Caching it is
        # what makes a twenty-PR repo cost one branch read instead of
        # twenty, but §29's guarantee is about the base at the moment of the
        # WRITE, and someone else may have moved it since. One uncached read
        # here -- at most one per merge, never one per PR -- keeps the rule
        # exactly as tight as it was. `moved` has already caught this
        # sweep's own merges above without any read at all.
        fresh = base_head(repo, base)
        if fresh != st.base_sha:
            green = False
            detail = (f"{STALE_BASE} — checks ran against {st.base_sha[:8]}, "
                      f"{base} is at {fresh[:8]}")

    if not green:
        # Self-heal the finance-owl cases: a conflicted dependabot PR never
        # un-dirties itself if dependabot has not noticed, and a stale one
        # never re-runs its checks (strict:false) until it is rebased.
        # Loop-produced auto/* PRs get no such kick — only their loop can
        # rewrite them — so a stale one is a review item like any other.
        kick = conflict_self_heals(pr) and (
            detail == CONFLICTING or detail.startswith(STALE_BASE))
        if kick:
            detail += request_rebase(repo, num)
        if detail.startswith(STALE_BASE) and kick:
            _file(s, "stale", r, detail, ledger.STALE)
        else:
            _file(s, "review", r, detail, ledger.ROUTED)
        return

    if not DRY_RUN:
        # --match-head-commit makes GitHub enforce the judged-head rule
        # server-side: if anything moved the branch between our read and
        # this call, the merge is refused instead of landing unjudged
        # content. A refused merge is not a failure — the next sweep
        # re-judges whatever the head is by then.
        code, _, err = sh("pr", "merge", str(num), "--repo", repo,
                          "--squash", "--match-head-commit", sha)
        if code != 0:
            _file(s, "skipped", r, f"merge refused: {err[:60]}", ledger.SKIPPED)
            return
    _file(s, "armed", r, f"{r.shape}; {detail}",
          ledger.WOULD_MERGE if DRY_RUN else ledger.MERGED,
          None if DRY_RUN else _merge_commit_sha(repo, num))
    # This merge moved the base. Every candidate still to come on it was
    # judged -- by GitHub, when its checks ran -- against the base that no
    # longer exists. #145 landed 21 seconds after #148 that way.
    s.moved.add((repo, base))


def _evaluate(pr, repo, num, data_dirs, s, cache=None):
    """Reach a verdict for one PR and file it. _read() then _decide().

    The serial form, for callers judging one PR at a time. sweep() runs the
    two halves apart so reads can overlap; the verdict is the same either
    way, because _read() never looks at the Sweep.
    """
    _decide(_read(pr, repo, num, data_dirs, cache), s)


def sweep(deadline=None, clock=time.monotonic, workers=None, record=True):
    """Reach a verdict on every open PR and return the Sweep. Prints nothing.

    `deadline` is a Budget: a `clock()` value past which the walk stops and
    returns what it has. Injected, and so is the clock, because a run that
    cannot be reproduced in a test is a run whose behaviour is a guess.

    Why a budget exists at all: every caller here runs under a supervisor with
    a hard timeout, and a walk that overruns is KILLED -- producing nothing at
    all, not a partial answer. An orchestrator saw exactly that, three jobs
    failing daily for two days while its queue sat undrained. Stopping at a
    budget and saying so is strictly better than being killed and saying
    nothing.

    A stopped sweep is not a failed one. It reports the verdicts it reached
    and `unreached` for the rest, the same way `failed` already separates "no
    verdict" from a decision -- an incomplete answer that admits it is
    incomplete.

    The budget is checked inside the enumeration too, and before each PR is
    decided. It used to be checked only once enumeration had returned, and
    enumeration -- ~110 serial `gh pr list` calls, a gh.exe spawn each on
    Windows -- came to take the whole budget: the 2026-10-06 22:17 run
    stopped on its first PR with 109 unreached and zero verdicts.

    Reads run `workers` at a time (GATE_WORKERS, default 8). Verdicts and
    writes run serially in queue order, after each PR's reads are in,
    because a verdict depends on which bases this sweep has already merged
    into (`moved`) and that is only known once every earlier PR is decided.
    Reads that are still in flight when the budget ends are abandoned: their
    PRs count as unreached, and a PR is still judged whole or not at all.

    Split out of main() so the queue has exactly one author. Anything else
    that wants to know what the gate thinks -- a triage list, a dashboard, a
    loop's summary count -- was previously rebuilding this walk from the
    primitives, which is how a dashboard came to show PRs as ready that the
    gate would refuse to merge that sweep: the rebuild predated the
    base-freshness rule and nobody noticed, because the two answers were
    never compared.

    Honours DRY_RUN exactly as main() does; `Sweep.moved` is filled in either
    mode, so a report describes the run a real one would perform.
    """
    data_dirs = load_data_dirs()
    gate_cfg = load_gate_config()
    vis = repo_visibility() if ONLY_PUBLIC else {}
    s = Sweep(record=record)
    workers = workers or DEFAULT_WORKERS

    # Before anything is decided: has any gate merge been reverted? A pause
    # found now must hold this very sweep's merges in that repo.
    rows = s.rows()
    pauses, s.unscanned = detect_reverts(rows)
    for p in pauses:
        rows.append(p)
        _record(s, p)

    e = enumerate_open_prs(deadline=deadline, clock=clock, workers=workers)
    s.repos_total = len(e.repos)
    s.repos_unreached = len(e.unreached)
    s.repos_unlisted = list(e.unlisted)
    if e.stopped_early:
        s.stopped_early = True

    considered = [pr for pr in e.prs
                  if is_loop_produced(pr["repository"]["nameWithOwner"],
                                      pr["number"], pr)
                  and (not ONLY_PUBLIC
                       or vis.get(pr["repository"]["nameWithOwner"]) == "PUBLIC")]

    cache = RepoCache(gate_cfg)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_read, pr, pr["repository"]["nameWithOwner"],
                               pr["number"], data_dirs, cache, stop)
                   for pr in considered]
        for i, (pr, fut) in enumerate(zip(considered, futures)):
            repo = pr["repository"]["nameWithOwner"]
            num = pr["number"]
            if deadline is not None and clock() >= deadline:
                # Checked BEFORE the PR is decided, never during: a PR is
                # judged whole or not at all. Half a verdict is the thing
                # `failed` exists to prevent. Reads already in hand for
                # later PRs are discarded unconsulted; reads still in
                # flight give up at their next call.
                s.stopped_early = True
                s.unreached = len(considered) - i
                stop.set()
                for f in futures[i:]:
                    f.cancel()
                break
            try:
                _decide(fut.result(), s)
            except ReadFailed as exc:
                # No verdict was reached. Say exactly that, in its own
                # section, rather than borrowing the wording of a decision.
                s.failed.append((repo, num, str(exc), pr["title"]))
                _record(s, ledger_row(Reading(pr, repo, num),
                                      ledger.NO_VERDICT, str(exc)))
    return s


def main():
    print(f"=== merge_gate :: {'REPORT ONLY' if DRY_RUN else 'APPLYING'} ===\n")
    if not DRY_RUN:
        try:
            ledger.require_writable()
        except ledger.LedgerError as exc:
            print(f"⚠ {exc}\nRefusing to merge without a merge record. "
                  f"Exit {EXIT_NO_LEDGER}.")
            return EXIT_NO_LEDGER
    budget = os.environ.get("SWEEP_BUDGET_SECONDS", "").strip()
    deadline = (time.monotonic() + float(budget)) if budget else None
    s = sweep(deadline)

    def dump(title, rows):
        print(f"=== {title} ({len(rows)}) ===")
        for repo, num, why, t in rows:
            print(f"  {repo}#{num:<4} {t[:52]:<52} :: {why}")
        print()

    dump("MERGED at judged head" if not DRY_RUN else "WOULD MERGE (judged head)", s.armed)
    # Green, allowlisted, and waiting only for dependabot to rebase onto the
    # base that moved -- this sweep or earlier. Not a review item: nothing
    # here needs a human, and the next sweep after the rebase merges it.
    dump("STALE BASE (rebase requested)", s.stale)
    dump("ROUTED to review", s.review)
    dump("SKIPPED (deliberately ungated repo)", s.skipped)
    # Not labelled on the PR: drift is a fact about the repo's settings, and
    # every PR in it would carry the same label for the same reason.
    dump("DRIFT (auto-merge on, no correctness gate — fix the repo)", s.drift)
    dump("HELD by circuit breaker (green; repo paused, rate-limited or base red)",
         s.held)
    if s.failed:
        dump("NO VERDICT — GitHub did not answer (retry; not a finding)", s.failed)

    if not DRY_RUN:
        for repo, num, _, _ in s.review:
            sh("pr", "edit", str(num), "--repo", repo, "--add-label", NEEDS_REVIEW)

    print(f"merged={len(s.armed)} stale={len(s.stale)} review={len(s.review)} "
          f"skipped={len(s.skipped)} drift={len(s.drift)} held={len(s.held)} "
          f"no_verdict={len(s.failed)}")
    if s.failed:
        # Loud, because a partial sweep that looks complete is how a drained
        # queue gets reported while a fifth of it was never examined.
        print(f"⚠ {len(s.failed)} PR(s) got NO verdict. This run is incomplete.")
    if s.ledger_errors:
        print(f"⚠ {len(s.ledger_errors)} ledger write(s) failed: "
              f"{s.ledger_errors[0]}")
    if s.repos_unlisted:
        # Not "no PRs there": GitHub did not answer for these repos, so
        # their PRs are simply absent from this walk (§22).
        print(f"⚠ {len(s.repos_unlisted)} repo(s) could not be listed; their "
              f"PRs were not examined: {', '.join(s.repos_unlisted[:6])}"
              f"{' …' if len(s.repos_unlisted) > 6 else ''}")
    if s.stopped_early:
        where = (f" and {s.repos_unreached} of {s.repos_total} repo(s) never "
                 "listed" if s.repos_unreached else "")
        print(f"⚠ STOPPED AT BUDGET with {s.unreached} PR(s) unreached{where}. "
              "This run is incomplete; the next sweep continues from the queue "
              "as it then stands.")
    if DRY_RUN:
        print("\nRe-run with DRY_RUN=0 to merge judged heads and apply labels.")
    if s.stopped_early and s.verdicts == 0:
        # A run that judged nothing must not exit like one that judged
        # everything. The scheduler reads the exit status, not the prose.
        print(f"\n⚠ No PR was judged before the budget ran out. Exit "
              f"{EXIT_NO_VERDICTS}: this sweep did not happen.")
        return EXIT_NO_VERDICTS
    return 0


if __name__ == "__main__":
    sys.exit(main())
