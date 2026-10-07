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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_pr import (  # noqa: E402
    DEFAULT_WORKERS, Abandoned, _run_gh, auto_mergeable, bail_if, bump_kind,
    changed_paths, classify, conflict_self_heals, content_veto,
    enumerate_open_prs, is_conflict, is_dependabot, load_data_dirs,
    promote_candidate, repo_visibility,
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
# Prose for the report, but compared by code: this token links check_state's
# conflict verdict to the self-heal in _evaluate. One definition, or a wording
# tweak silently disables the rebase kick with no failure signal.
CONFLICTING = "CONFLICTING with base"
# Same contract: a prefix _evaluate matches on to decide who gets the rebase
# kick. Reworded here, silently nobody is asked to rebase.
STALE_BASE = "STALE BASE"
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
    Anything else raises, because the absence of an answer must never be
    rendered as one.
    """
    code, out, err = sh(*args)
    if code == 0:
        return out
    low = err.lower()
    if "404" in low or "not found" in low or "not protected" in low:
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

    def __init__(self):
        self._vals, self._locks = {}, {}
        self._lock = threading.Lock()

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

    def __init__(self, repo, number, sha, base, base_sha, draft, mstate):
        self.repo, self.number = repo, number
        self.sha, self.base, self.base_sha = sha, base, base_sha
        self.draft = draft.strip().lower() == "true"
        self.conflict = is_conflict(mstate)
        self.allow_auto: bool | None = None       # repo permits auto-merge
        self.required: list | None = None         # required contexts on base
        self.missing: list | None = None          # required, no check-run
        self.failed: list | None = None           # required, run is red
        self.branch_head: str | None = None       # where base was when read


def read_state(repo, number, cache=None, stop=None):
    """Read everything judge() needs about one PR. Raises ReadFailed.

    Per-repo facts go through `cache`; per-PR facts are read every time.
    Short-circuits in the same order as the old check_state(), so a draft or
    an ungated repo still costs one call, not five.
    """
    cache = cache if cache is not None else RepoCache()
    out = sh_strict("api", f"repos/{repo}/pulls/{number}",
                    "--jq", ".head.sha,.base.ref,.draft,.mergeable_state,.base.sha")
    if out is None:
        raise ReadFailed(f"{repo}#{number}: pull request not found")
    lines = out.splitlines()
    if len(lines) < 5:
        raise ReadFailed(f"{repo}#{number}: short response from pulls endpoint")
    sha, base, draft, mstate, base_sha = lines[:5]
    st = State(repo, number, sha, base, base_sha, draft, mstate)

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
    if not st.required:
        return st

    # An armed PR that goes CONFLICTING sits forever: auto-merge can never
    # fire on a dirty PR, however green its checks. finance-owl#69–79 sat
    # exactly there on 2026-08-15 — five armed bumps, required checks green,
    # all dirty over the shared lockfile — until rebases were requested by
    # hand. (Why only "dirty" counts: see is_conflict.)
    if st.conflict:
        return st

    bail_if(stop)
    out = sh_strict("api", f"repos/{repo}/commits/{sha}/check-runs",
                    "--jq", "[.check_runs[]|{name,conclusion}]")
    if out is None:
        raise ReadFailed(f"{repo}#{number}: check-runs not found for {sha[:8]}")
    # An empty list here is a real answer: no check has reported yet. That
    # lands in judge() as "required check(s) not run", which blocks.
    runs = json.loads(out) if out else []
    by_name = {r["name"]: r["conclusion"] for r in runs}
    st.missing = [c for c in st.required if c not in by_name]
    st.failed = [c for c in st.required
                 if by_name.get(c) not in (None, "success", "skipped")
                 and c in by_name]
    if st.missing or st.failed:
        return st

    # Green -- against which base? Read only for a green PR, so a red one
    # still costs what it used to; cached, so a repo's base is read once
    # however many green PRs sit on it.
    bail_if(stop)
    st.branch_head = cache.get(("base_head", repo, base),
                               lambda: base_head(repo, base))
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
    if not st.required:
        return None, f"no required checks on {st.base}"
    if st.conflict:
        return False, CONFLICTING
    if st.missing:
        return False, f"required check(s) not run: {', '.join(st.missing)}"
    if st.failed:
        return False, f"required check(s) red: {', '.join(st.failed)}"
    # Checked last so a red PR is still reported as red: that is the finding
    # a reader can act on, and a rebase would only re-run the same red checks.
    stale = stale_base_reason(st.repo, st.base, st.base_sha, moved,
                              head=st.branch_head)
    if stale:
        return False, stale
    return True, f"green: {', '.join(st.required)}"


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

    def __init__(self):
        self.armed, self.review, self.skipped = [], [], []
        self.stale, self.failed = [], []
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

    @property
    def verdicts(self):
        """Rows that state a decision. `failed` is not one: it says GitHub
        did not answer, which is the absence of a verdict, not a verdict."""
        return len(self.armed) + len(self.stale) + len(self.review) + len(self.skipped)


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


def _read(pr, repo, num, data_dirs, cache=None, stop=None):
    """The read half of one PR's judgement. Raises ReadFailed.

    Safe to run concurrently with other PRs' reads: it touches nothing on
    the Sweep and writes nothing to GitHub. `stop` lets a read running ahead
    of the budget give up between calls once the budget is gone.
    """
    cache = cache if cache is not None else RepoCache()
    r = Reading(pr, repo, num)
    bail_if(stop)
    paths = changed_paths(repo, num)
    title, body = r.title, pr.get("body", "") or ""
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
    out = sh_strict("api", f"repos/{repo}/pulls/{num}",
                    "--jq", ".auto_merge != null")
    r.armed = out is not None and out.strip() == "true"

    if r.mergeable:
        r.state = read_state(repo, num, cache, stop)
    return r


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
        s.review.append((repo, num, why, title))
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
        s.skipped.append((repo, num, detail, title))
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
            s.stale.append((repo, num, detail, title))
        else:
            s.review.append((repo, num, detail, title))
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
            s.skipped.append((repo, num, f"merge refused: {err[:60]}", title))
            return
    s.armed.append((repo, num, f"{r.shape}; {detail}", title))
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


def sweep(deadline=None, clock=time.monotonic, workers=None):
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
    vis = repo_visibility() if ONLY_PUBLIC else {}
    s = Sweep()
    workers = workers or DEFAULT_WORKERS

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

    cache = RepoCache()
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
    return s


def main():
    print(f"=== merge_gate :: {'REPORT ONLY' if DRY_RUN else 'APPLYING'} ===\n")
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
    if s.failed:
        dump("NO VERDICT — GitHub did not answer (retry; not a finding)", s.failed)

    if not DRY_RUN:
        for repo, num, _, _ in s.review:
            sh("pr", "edit", str(num), "--repo", repo, "--add-label", NEEDS_REVIEW)

    print(f"merged={len(s.armed)} stale={len(s.stale)} review={len(s.review)} "
          f"skipped={len(s.skipped)} no_verdict={len(s.failed)}")
    if s.failed:
        # Loud, because a partial sweep that looks complete is how a drained
        # queue gets reported while a fifth of it was never examined.
        print(f"⚠ {len(s.failed)} PR(s) got NO verdict. This run is incomplete.")
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
