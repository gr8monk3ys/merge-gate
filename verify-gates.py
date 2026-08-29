#!/usr/bin/env python3
"""Verify every required status check ACTUALLY runs on pull requests.

A required check that never executes is worse than no check: it turns green
never, so the PR can never merge. TAlker sat in exactly that state — `master`
required "security-baseline / security-baseline" while only CodeQL Analyze ever
ran, so no PR could merge at all.

Marking a check required is therefore only safe if that check fires on
`pull_request` events, not just on `push` to the default branch. This script
confirms that against real PR head commits.
"""

import json
import subprocess
import sys

import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_pr import require_owners  # noqa: E402


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    return (r.stdout.strip() if r.returncode == 0 else None)


def split_repo(arg):
    """Accept `owner/repo`, or a bare `repo` resolved across every OWNERS entry.

    The fleet is not all under one account: the four Vivance apps live in the
    Vivancedata org. Assuming a single owner meant every one of them resolved to
    a 404 and was skipped, so this script has never actually checked them.
    Falling back to OWNERS[0] on a genuine miss keeps the caller's error message
    about the repo rather than about this resolution step.
    """
    if "/" in arg:
        owner, _, name = arg.partition("/")
        return owner, name
    owners = require_owners()
    for owner in owners:
        if gh("api", f"repos/{owner}/{arg}", "--jq", ".full_name"):
            return owner, arg
    return owners[0], arg


def main():
    repos = sys.argv[1:] or []
    if not repos:
        print("usage: verify-gates.py <owner/repo | repo> [...]")
        return 2

    problems = 0
    for arg in repos:
        owner, repo = split_repo(arg)
        label = f"{owner}/{repo}"
        d = gh("api", f"repos/{owner}/{repo}", "--jq", ".default_branch")
        if not d:
            # Unreadable is NOT clean. Counting a skip as a pass is how a
            # verifier reports "0 repo(s) at deadlock risk" having verified
            # nothing at all -- the exact false confidence this script exists
            # to prevent.
            problems += 1
            print(f"{label:<34} UNVERIFIED (cannot read repo -- wrong owner, or no access)")
            continue
        req_raw = gh("api", f"repos/{owner}/{repo}/branches/{d}/protection",
                     "--jq", ".required_status_checks.contexts")
        req = json.loads(req_raw) if req_raw else []
        if not req:
            print(f"{label:<34} no required checks")
            continue

        # Look at up to 3 recent PRs; a check counts as "runs on PRs" if it
        # appears on ANY of them. One PR is not enough evidence — a single
        # draft or docs-only PR may legitimately skip a path-filtered job.
        nums_raw = gh("pr", "list", "--repo", label, "--state", "all",
                      "--limit", "3", "--json", "number", "--jq", ".[].number")
        nums = [n for n in (nums_raw or "").splitlines() if n.strip()]
        if not nums:
            # Required checks with no PR ever opened cannot be confirmed to
            # fire. Report it rather than implying the gates are sound.
            problems += 1
            print(f"{label:<34} UNVERIFIED (no PRs to sample) required={req}")
            continue

        seen = set()
        for n in nums:
            sha = gh("api", f"repos/{owner}/{repo}/pulls/{n}", "--jq", ".head.sha")
            if not sha:
                continue
            names = gh("api", f"repos/{owner}/{repo}/commits/{sha}/check-runs",
                       "--jq", "[.check_runs[].name]")
            if names:
                seen.update(json.loads(names))

        missing = [c for c in req if c not in seen]
        if missing:
            problems += 1
            print(f"{label:<34} DEADLOCK RISK — required but never seen on PRs: {missing}")
            print(f"{'':<34}   checks that DO run: {sorted(seen)}")
        else:
            print(f"{label:<34} ok — all required checks run on PRs: {req}")

    print(f"\n{problems} repo(s) at deadlock risk or unverified")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
