"""The auto-fix cap: a PR whose required checks were red on too many
different heads stops being an auto-fix job and becomes a human's.

pr-shepherd pushes a fix whenever a PR's checks are red. Nothing counted
those pushes, so a fix that never converges could run every day for weeks.
The gate already records the head of every verdict in its ledger; the
number of distinct red heads is the number of fixes that did not work.

Pure, like test_review.py: every GitHub read is a stand-in.
"""
import gate_ledger as ledger
import merge_gate as mg
from test_review import _evaluate
from test_trust import _run

RED = [_run("ci", "failure")]


def _prior_red(heads, repo="o/r", pr=7):
    for h in heads:
        ledger.append({"event": "verdict", "repo": repo, "pr": pr,
                       "head_sha": h, "decision": "ROUTED",
                       "reason": "required check(s) red: ci"})


def test_a_red_pr_under_the_cap_is_routed_as_red(monkeypatch):
    _prior_red(["h1"])
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True}, runs=RED, review=None)
    [(_, _, detail, _)] = s.review
    assert detail == "required check(s) red: ci"


def test_the_third_red_head_hits_the_default_cap(monkeypatch):
    _prior_red(["h1", "h2"])
    s, _ = _evaluate(monkeypatch, {"allow_reviewed": True}, runs=RED, review=None)
    [(_, _, detail, _)] = s.review
    assert detail.startswith(f"{mg.FIX_CAP} — required checks red on 3 heads (cap 3)")
    assert detail.endswith("required check(s) red: ci")


def test_the_same_head_seen_twice_is_one_attempt(monkeypatch):
    _prior_red(["h1", "h1", "h7", "h7"])
    s, _ = _evaluate(monkeypatch, {"allow_reviewed": True}, runs=RED, review=None)
    [(_, _, detail, _)] = s.review
    assert not detail.startswith(mg.FIX_CAP)


def test_other_prs_and_repos_do_not_count(monkeypatch):
    _prior_red(["h1", "h2"], pr=8)
    _prior_red(["h3", "h4"], repo="o/other")
    s, _ = _evaluate(monkeypatch, {"allow_reviewed": True}, runs=RED, review=None)
    [(_, _, detail, _)] = s.review
    assert not detail.startswith(mg.FIX_CAP)


def test_repos_yml_sets_the_cap(monkeypatch):
    _prior_red(["h1"])
    s, _ = _evaluate(monkeypatch, {"allow_reviewed": True, "max_fix_attempts": 2},
                     runs=RED, review=None)
    [(_, _, detail, _)] = s.review
    assert detail.startswith(f"{mg.FIX_CAP} — required checks red on 2 heads (cap 2)")


def test_an_unreadable_cap_falls_back_to_the_default_not_to_unlimited():
    assert mg.max_fix_attempts({"max_fix_attempts": "lots"}) == mg.DEFAULT_MAX_FIX_ATTEMPTS
    assert mg.max_fix_attempts({}) == mg.DEFAULT_MAX_FIX_ATTEMPTS


def test_a_capped_pr_still_never_merges_once_green(monkeypatch):
    # The cap only relabels a red verdict; a green head is judged as ever.
    _prior_red(["h1", "h2", "h3"])
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True})
    assert len(s.armed) == 1
