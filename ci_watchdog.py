#!/usr/bin/env python3
"""Liveness check: is each repo's default-branch CI still green, and since when?

The fleet tracks what its agents PRODUCE and never whether the environment
those agents produce into is still alive. Those are different questions, and
the gap cost 37 days:

  album-conceptualizer's CI broke on 2026-07-10 (commit 519e2156, titled
  "ci: minute-safe Actions policy", which also deleted the npm entry from
  dependabot.yml). Every PR opened afterwards failed its required checks, so
  nothing could merge. 15 policy-clean dependabot PRs piled up behind it and
  5 more were orphaned into permanently-unrebaseable zombies. Nothing
  reported any of it, because a repo with a dead pipeline and a repo with
  nothing to do look identical from the outside: both are quiet.

A red default branch is upstream of everything else this fleet measures. The
merge gate can only ever say "required check red" one PR at a time; it cannot
say "this repo has been broken for a month", because it has no memory and no
notion of the default branch's own health.

Severity is set by CONSEQUENCE, not by how loud the failure looks. A red
workflow nothing gates on blocks no merge -- half the fleet's
org-release-please runs are startup_failure right now and always have been.
Only a required check red at the default branch's HEAD actually holds the
queue, so only that escalates:

  BROKEN    a workflow is failing, it has been for >= STALE_DAYS, AND
            required checks are red at HEAD. The album-conceptualizer case;
            every merge in the repo is blocked.
  STARTUP   a gated workflow could not start at all. Usually an invalid
            workflow file or exhausted Actions minutes; on this account read
            it as minutes first (see DECISIONS.md).
  RED       same as BROKEN, but red only recently -- may be just this commit.
  SILENT    no default-branch push run in >= SILENT_DAYS. A pipeline that
            stopped firing is invisible to every other check in this repo:
            dotfiles has had none in 159 days.
  NOTE      failing, but nothing is gated on it. Worth reading, not fixing
            tonight.

Repos with no CI at all are reported separately, not as failures: that is a
gating decision (see verify-gates.py), not a regression.

Read-only. Exits 1 only on BROKEN, so a routine can gate on it without being
woken by a release workflow nobody merges against.

Usage:
    GATE_OWNERS=you python3 ci_watchdog.py       # every non-archived repo
    python3 ci_watchdog.py trading-bot           # named repos only
    STALE_DAYS=3 python3 ci_watchdog.py          # tighten the threshold
"""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_pr import _run_gh, all_repos  # noqa: E402
from merge_gate import ReadFailed, required_checks  # noqa: E402

STALE_DAYS = int(os.environ.get("STALE_DAYS", "2"))
SILENT_DAYS = int(os.environ.get("SILENT_DAYS", "30"))
NOW = dt.datetime.now(dt.timezone.utc)


def _age_days(stamp):
    """Whole days between an ISO8601 GitHub timestamp and now."""
    when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (NOW - when).days


def default_branch(repo):
    """The repo's default branch, or None if GitHub did not say.

    Never guesses "main". Several repos in this fleet default to `master`,
    and an earlier cut fell back to "main" on a failed read -- which measures
    a branch that does not exist and reports the repo as having no CI at all.
    """
    r = _run_gh(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def default_branch_runs(repo, branch, limit=100):
    """Completed CI runs on the repo's default branch, newest first.

    Two filters, both load-bearing:

    branch  PR-event runs come from head commits, so without this one broken
            feature branch masquerades as a broken repo. It is the default
            branch's own health being measured.
    event   `push` only. Dependabot's update jobs run as `dynamic` and land
            on the default branch too -- the first cut of this script read
            "npm_and_yarn in /apps/web" as album-conceptualizer's CI and
            called a healthy repo BROKEN. A failed dependency-update job is
            worth knowing about, but it blocks no merge.
    """
    # The filters are QUERY PARAMETERS, not Python. per_page bounds the
    # window BEFORE filtering, so filtering afterwards judges a repo on
    # whatever survives: lscaturchio.xyz's newest 100 runs were 97 schedule
    # jobs spanning one day, 0 rows survived, and a repo with 302 real CI
    # runs was filed as "runs no CI at all". Five repos got that verdict.
    r = _run_gh(["gh", "api",
                 f"repos/{repo}/actions/runs?branch={branch}&event=push"
                 f"&status=completed&per_page={limit}",
                 "--jq", "[.workflow_runs[]|{name,conclusion,at:.updated_at}]"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def required_red(repo, branch):
    """(red_contexts, total, where) for the repo's gating checks.

    Sampled from an OPEN PR HEAD, never from the default branch. Required
    contexts fire on pull_request events, so at the default branch's own
    head they legitimately do not exist -- and a previous cut that sampled
    there declared finance-owl "2/2 required RED" and
    elementary-teacher-website "5/5 RED" while both were green on their PR
    heads. That is the exact trap DECISIONS.md documents ("required checks
    must be sampled from a PR head commit"), reproduced by the tool built
    to catch CI drift.

    Returns:
      (None, 0,  "")       branch genuinely unprotected -- a gating decision
      (None, n,  "")       gated, but no open PR to sample -- UNVERIFIABLE,
                           which is not the same claim as red or green
      (list, n, "PR #x")   the verdict, and where it was measured

    required_checks is reused for its error semantics: [] only on a real
    404, ReadFailed when GitHub did not answer. Only explicit failures
    count as red here -- a pending or not-yet-reported check on a PR head
    is "not landed", not "broken", and this script alarms rather than arms,
    so it wants precision where merge_gate wants strictness.
    """
    required = required_checks(repo, branch)
    if not required:
        return None, 0, ""
    p = _run_gh(["gh", "api",
                 f"repos/{repo}/pulls?state=open&sort=updated"
                 "&direction=desc&per_page=1",
                 "--jq", '.[0] // empty | "\\(.number) \\(.head.sha)"'])
    if p.returncode != 0:
        raise ReadFailed(f"{repo}: could not list open PRs")
    if not p.stdout.strip():
        return None, len(required), ""
    num, sha = p.stdout.strip().split()
    s = _run_gh(["gh", "api",
                 f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                 "--jq", "[.check_runs[]|{name,conclusion}]"])
    if s.returncode != 0 or not s.stdout.strip():
        raise ReadFailed(f"{repo}: check-runs unreadable at {sha[:8]}")
    try:
        runs = json.loads(s.stdout)
    except json.JSONDecodeError:
        raise ReadFailed(f"{repo}: unparseable check-runs")
    by_name = {}
    for c in runs:            # newest first; a rerun must beat its ancestor
        by_name.setdefault(c["name"], c["conclusion"])
    red = [c for c in required
           if by_name.get(c) in ("failure", "timed_out", "cancelled",
                                 "action_required", "startup_failure")]
    return red, len(required), f"PR #{num}"


def verdict(repo):
    """(severity, headline) for one repo. severity None means healthy.

    Judged per workflow, not per run. A repo runs several workflows on push
    (CI, codeql, trivy, release-please); one of them being red says something
    quite different from all of them being red, and collapsing the two hides
    the case that matters. The headline reports the workflow that has gone
    longest without a green run, because that is the one holding merges.
    """
    branch = default_branch(repo)
    if branch is None:
        return "?", "could not read the default branch"
    runs = default_branch_runs(repo, branch)
    if runs is None:
        return "?", "could not read Actions runs"
    if not runs:
        return "NOCI", f"no CI runs on {branch}"

    # "No runs lately" is only a finding if there was something TO run on.
    # A dormant repo has no pushes and therefore no push runs, and the
    # 2026-07-10 minute diet deliberately removed push triggers fleet-wide --
    # an earlier cut flagged 17 repos this way, every one of them either idle
    # or intentionally configured that way. The signal worth having is
    # narrower: commits landed and CI did not fire.
    quiet = _age_days(runs[0]["at"])
    if quiet >= SILENT_DAYS:
        c = _run_gh(["gh", "api", f"repos/{repo}/commits?per_page=1",
                     "--jq", ".[0].commit.committer.date"])
        if c.returncode != 0 or not c.stdout.strip():
            return None, f"no recent runs ({quiet}d) and no commit data"
        pushed = _age_days(c.stdout.strip())
        if pushed >= quiet:
            return None, f"idle ({pushed}d since last commit)"
        return "SILENT", (f"commits {pushed}d ago but no CI run in {quiet}d — "
                          f"trigger removed or scheduler stopped")

    by_workflow = {}
    for r in runs:                       # runs are newest-first
        by_workflow.setdefault(r["name"], []).append(r)

    failures, startups = [], []          # (days_since_green, name)
    for name, history in by_workflow.items():
        if history[0]["conclusion"] == "success":
            continue                     # this workflow is currently fine
        if history[0]["conclusion"] == "startup_failure":
            startups.append(name)
            continue
        # The useful number is not "how long since it broke" but "how long
        # since it last worked" -- that is the window in which no PR gated
        # on this workflow could merge, and it is exactly what nobody was
        # measuring for 37 days. None = no green run on record at all.
        green = next((x for x in history
                      if x["conclusion"] == "success"), None)
        failures.append((_age_days(green["at"]) if green else None, name))

    if not failures and not startups:
        return None, f"green ({quiet}d ago)"

    # Headline the genuinely-failing workflow when there is one; a workflow
    # that cannot START (usually org-release-please, fleet-wide) only leads
    # when nothing else is wrong, so it cannot shadow the one holding
    # merges. "Never green" sorts above any finite age.
    if failures:
        days, name = max(failures, key=lambda f: (f[0] is None, f[0] or 0))
        sev = "BROKEN" if days is None or days >= STALE_DAYS else "RED"
        detail = (f"{name[:30]} failing; no green run on record"
                  if days is None else
                  f"{name[:30]} failing; last green {days}d ago")
    else:
        sev = "STARTUP"
        detail = (f"{startups[0][:30]} could not start — invalid workflow "
                  "or exhausted Actions minutes")

    # Severity is set by consequence, not by noise. A workflow nothing gates
    # on can be red for a year without blocking a single merge -- half the
    # fleet's org-release-please runs are startup_failure today. Only a red
    # required check on a real PR head actually holds the queue.
    red, total, where = required_red(repo, branch)
    if total == 0:
        sev = "NOTE"                     # unprotected branch: nothing gated
    elif red is None:
        # Gated, but no open PR exists to measure against. Nothing is
        # concretely blocked today, and claiming BROKEN on an unmeasured
        # repo is how false positives got shipped last time.
        sev = "NOTE"
        detail += f" (gated ×{total}, unverifiable — no open PR)"
    elif red:
        detail += f" [{len(red)}/{total} required red at {where}]"
    else:
        sev = "NOTE"                     # required checks green where PRs live
        detail += f" (required checks green at {where})"
    return sev, detail


# Every severity verdict() can return must appear here: main() indexes it
# directly rather than using .get, so a missing key is a crash on the one
# repo that produces it, mid-sweep, after all the API calls are spent.
ORDER = {"BROKEN": 0, "STARTUP": 1, "SILENT": 2, "RED": 3, "NOTE": 4, "?": 5}


def main():
    wanted = set(sys.argv[1:])
    repos = [r for r in all_repos()
             if not wanted or r.split("/")[1] in wanted]
    flagged, healthy, no_ci = [], 0, []

    for repo in repos:
        try:
            sev, why = verdict(repo)
        except ReadFailed as e:
            # A verdict was never reached. Say that, rather than letting a
            # dropped read read as "nothing wrong here".
            flagged.append(("?", repo, str(e)))
            continue
        if sev == "NOCI":
            no_ci.append(repo)
        elif sev is None:
            healthy += 1
        else:
            flagged.append((sev, repo, why))

    flagged.sort(key=lambda f: (ORDER[f[0]], f[1]))
    print(f"=== ci_watchdog :: {len(repos)} repos, "
          f"stale after {STALE_DAYS}d ===\n")
    for sev, repo, why in flagged:
        print(f"  {sev:8s} {repo.split('/')[1]:30s} {why}")
    if not flagged:
        print("  every repo's default branch is green")
    if no_ci:
        print(f"\n  ({len(no_ci)} repo(s) run no CI at all — a gating "
              f"decision, not a regression: "
              f"{', '.join(r.split('/')[1] for r in no_ci[:6])}"
              f"{' …' if len(no_ci) > 6 else ''})")

    # BROKEN and STARTUP survive to here only with required checks verified
    # red on an open PR head -- everything unverified or ungated was demoted
    # to NOTE in verdict() -- so both mean "merges are blocked, measured".
    blocking = [f for f in flagged if f[0] in ("BROKEN", "STARTUP")]
    print(f"\nblocking={len(blocking)} other={len(flagged) - len(blocking)} "
          f"healthy={healthy}")
    if blocking:
        print("⚠ A red default branch blocks every merge in that repo. "
              "album-conceptualizer sat here for 37 days unnoticed.")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
