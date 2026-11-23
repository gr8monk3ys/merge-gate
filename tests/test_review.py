"""The reviewed path: a PR outside the shape allowlist may merge when its repo
opted in and a reviewer passed the exact commit the gate would merge.

Pure, like test_trust.py: every GitHub read is a stand-in.
"""
import pytest

import classify_pr as cp
import gate_ledger as ledger
import merge_gate as mg
from test_trust import _gh, _merges, _repo, _run

REVIEW = "commits/h7/statuses"


def _loop_pr(num=7):
    return {"number": num, "title": "feat: a slice", "body": "",
            "author": {"login": "owner"}, "labels": [{"name": "automated"}],
            "headRefName": "auto/finish-20261115"}


def _evaluate(monkeypatch, cfg, *, review="success", paths=("src/x.py",),
              runs=None, extra=()):
    """Judge loop PR #7 on o/r in a real run; returns (sweep, writes)."""
    runs = runs if runs is not None else [_run("ci", "success")]
    answers = list(extra) + _repo(["ci"], runs)
    if review is not None:
        answers.append((REVIEW, review))
    writes = []
    monkeypatch.setattr(mg, "sh_strict", _gh(answers))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: list(paths) if paths else None)
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(_loop_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": cfg}))
    return s, writes


def test_the_opt_ins_are_literal_true_only(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("repos:\n"
                 "  a: { allow_reviewed: true, allow_reviewed_content: true }\n"
                 "  b: { allow_reviewed: 'yes', allow_reviewed_content: 1 }\n"
                 "  c: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert (cfg["a"]["allow_reviewed"], cfg["a"]["allow_reviewed_content"]) == (True, True)
    assert (cfg["b"]["allow_reviewed"], cfg["b"]["allow_reviewed_content"]) == (False, False)
    assert (cfg["c"]["allow_reviewed"], cfg["c"]["allow_reviewed_content"]) == (False, False)


def test_a_reviewed_pr_merges_at_the_head_the_review_names(monkeypatch):
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True})
    assert len(s.armed) == 1
    [merge] = _merges(writes)
    assert merge[merge.index("--match-head-commit") + 1] == "h7"
    [row] = ledger.read()[0]
    assert (row["decision"], row["shape"], row["review"]) == ("MERGED", "mixed", "success")
    assert row["reason"].startswith("mixed (reviewed); green")


def test_without_the_opt_in_no_review_is_read_and_nothing_changes(monkeypatch):
    writes = []
    monkeypatch.setattr(mg, "sh_strict", _gh([(".auto_merge != null", "false")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["src/x.py"])
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(_loop_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": {}}))
    assert s.armed == [] and _merges(writes) == []
    assert s.review[0][2] == "shape=mixed"
    assert ledger.read()[0][0]["review"] is None


@pytest.mark.parametrize("posted, said", [
    ("", "not posted"), ("failure", "failure"), ("error", "error"),
    ("pending", "pending")])
def test_a_green_pr_without_a_passing_review_routes_and_says_why(monkeypatch, posted, said):
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True}, review=posted)
    assert s.armed == [] and _merges(writes) == []
    assert s.review[0][2] == f"shape=mixed; green, but {mg.REVIEW_CONTEXT} is {said} at h7"


def test_the_review_does_not_replace_the_checks(monkeypatch):
    # No review answer is served: a red PR's review is never read.
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True}, review=None,
                          runs=[_run("ci", "failure")])
    assert s.armed == [] and _merges(writes) == []
    assert s.review[0][2] == "required check(s) red: ci"


def test_content_needs_its_own_opt_in(monkeypatch):
    post = ("content/posts/a.md",)
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True}, review=None, paths=post,
                          extra=[(".auto_merge != null", "false")])
    assert _merges(writes) == [] and s.review[0][2] == "content veto"

    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True,
                                        "allow_reviewed_content": True}, paths=post)
    assert len(_merges(writes)) == 1


def test_content_alone_is_not_an_opt_in(monkeypatch):
    s, writes = _evaluate(monkeypatch, {"allow_reviewed_content": True}, review=None,
                          extra=[(".auto_merge != null", "false")])
    assert _merges(writes) == [] and s.review[0][2] == "shape=mixed"


def test_an_unreadable_diff_is_never_reviewed(monkeypatch):
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True,
                                        "allow_reviewed_content": True},
                          review=None, paths=None,
                          extra=[(".auto_merge != null", "false")])
    assert _merges(writes) == []
    assert s.review[0][2].startswith("UNREADABLE")


def test_a_review_that_cannot_be_read_is_not_an_answer(monkeypatch):
    def boom(joined):
        raise mg.ReadFailed("HTTP 502")
    with pytest.raises(mg.ReadFailed):
        _evaluate(monkeypatch, {"allow_reviewed": True}, review=boom)


def test_the_breakers_still_hold_a_reviewed_pr(monkeypatch):
    s, writes = _evaluate(monkeypatch, {"allow_reviewed": True,
                                        "merge_gate_paused": True})
    assert _merges(writes) == [] and len(s.held) == 1


# --- a tests-only PR the shape rules decline is reviewable like any other ---

from test_trust import _new, _real, _tests_only, WF  # noqa: E402


def _with_review(monkeypatch, state):
    """Serve `state` as the review on head hA, on top of what is patched."""
    inner = mg.sh_strict
    monkeypatch.setattr(mg, "sh_strict", lambda *a: state if
                        "commits/hA/statuses" in " ".join(a) else inner(*a))


def test_a_tests_only_pr_in_a_repo_without_that_opt_in_takes_the_reviewed_path(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")])
    _with_review(monkeypatch, "success")
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, mg.RepoCache({"r": {"allow_reviewed": True}}))
    assert len(_merges(writes)) == 1
    assert s.armed[0][2].startswith("tests-only (reviewed); green")


def test_a_tests_only_pr_the_veto_rejects_takes_the_reviewed_path(monkeypatch):
    # An edit to an existing test file: never mergeable by shape, whatever CI counts.
    changed = {"filename": "tests/test_old.py", "status": "modified", "patch": "@@\n+x\n"}
    pr, _ = _tests_only(monkeypatch, [changed])
    _with_review(monkeypatch, "success")
    writes = _real(monkeypatch)
    cfg = {"allow_tests_only": True, "verify_workflow": WF, "allow_reviewed": True}
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, mg.RepoCache({"r": cfg}))
    assert len(_merges(writes)) == 1


def test_a_declined_tests_only_pr_without_a_review_keeps_its_reason(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")])
    _with_review(monkeypatch, "")
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, mg.RepoCache({"r": {"allow_reviewed": True}}))
    assert _merges(writes) == []
    assert s.review[0][2].startswith("tests-only auto-merge is off for this repo")
    assert s.review[0][2].endswith(f"green, but {mg.REVIEW_CONTEXT} is not posted at hA")


# --- required checks a repo declares, where GitHub cannot hold any ----------

def _unprotected(runs):
    """PR #7 on a base with no branch protection (a private repo, free plan)."""
    return _repo([], runs)


def test_declared_checks_gate_a_repo_github_cannot_protect(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_unprotected([_run("ci", "success")])))
    cache = mg.RepoCache({"r": {"gate_required_checks": ["ci"]}})
    green, why = mg.judge(mg.read_state("o/r", 7, cache))
    assert green is True and why == "green: ci"


def test_a_declared_check_that_did_not_run_blocks(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_unprotected([_run("lint", "success")])))
    cache = mg.RepoCache({"r": {"gate_required_checks": ["ci"]}})
    assert mg.judge(mg.read_state("o/r", 7, cache)) == (
        False, "required check(s) not run: ci")


def test_without_a_declaration_an_unprotected_repo_is_still_drift(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_unprotected([_run("ci", "success")])))
    green, why = mg.judge(mg.read_state("o/r", 7, mg.RepoCache({"r": {}})))
    assert green is None and why.startswith(mg.DRIFT)


def test_githubs_own_required_checks_win_over_the_declaration(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["build"], [_run("ci", "success")])))
    cache = mg.RepoCache({"r": {"gate_required_checks": ["ci"]}})
    assert mg.judge(mg.read_state("o/r", 7, cache)) == (
        False, "required check(s) not run: build")


def test_declared_checks_parse_as_a_list_of_names(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("repos:\n  a: { gate_required_checks: [ci, 'Lint, Test & Build'] }\n  b: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["gate_required_checks"] == ["ci", "Lint, Test & Build"]
    assert cfg["b"]["gate_required_checks"] == []


# --- a PR an owner opened is in the queue of a repo that opted in ------------

def _owner_pr(login="me", num=7, draft=False):
    return {"number": num, "title": "feat: mine", "body": "", "isDraft": draft,
            "author": {"login": login}, "labels": [], "headRefName": "feat/mine",
            "repository": {"nameWithOwner": "o/r"}}


def test_an_owners_pr_joins_the_queue_only_where_the_repo_opted_in(monkeypatch):
    monkeypatch.setattr(cp, "OWNERS", ["Me", "my-org"])
    opted, plain = {"r": {"allow_reviewed": True}}, {"r": {}}
    assert mg.in_queue(_owner_pr(), opted) is True          # login match ignores case
    assert mg.in_queue(_owner_pr(), plain) is False
    assert mg.in_queue(_owner_pr("a-stranger"), opted) is False
    loop = dict(_loop_pr(), repository={"nameWithOwner": "o/r"})
    assert mg.in_queue(loop, plain) is True                 # the fleet's own output, as before


def test_an_owners_pr_merges_on_a_review_never_on_its_shape(monkeypatch):
    # A lockfile-only diff: a loop's would merge on shape alone.
    cfg = {"allow_reviewed": True}
    answers = _repo(["ci"], [_run("ci", "success")])
    for review, merged in (("", 0), ("success", 1)):
        writes = []
        monkeypatch.setattr(mg, "sh_strict", _gh(answers + [(REVIEW, review)]))
        monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
        monkeypatch.setattr(mg, "DRY_RUN", False)
        monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
        s = mg.Sweep()
        mg._evaluate(_owner_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": cfg}))
        assert len(_merges(writes)) == merged, review
    assert ledger.read()[0][-1]["source"] == "other"


def test_declared_checks_cannot_vouch_for_a_pr_that_edits_the_workflows(monkeypatch):
    # GitHub is not enforcing these checks, and this PR rewrites what produces them.
    # Judged from the compare of the exact range merged, not the PR's path
    # list: here the list never mentions the CI file the range contains.
    import json
    cfg = {"allow_reviewed": True, "gate_required_checks": ["ci"]}
    renamed = {"filename": "ci/x.yml", "previous_filename": ".github/workflows/ci.yml"}
    for files, merged in (([{"filename": "src/x.py"}, {"filename": ".github/actions/a/action.yml"}], 0),
                          ([{"filename": "src/x.py"}, renamed], 0),
                          ([{"filename": "src/x.py"}], 1)):
        paths = ("src/x.py",)
        writes = []
        monkeypatch.setattr(mg, "sh_strict", _gh(
            [("compare/base0...h7", json.dumps({"files": files}))]
            + _unprotected([_run("ci", "success")]) + [(REVIEW, "success")]))
        monkeypatch.setattr(mg, "changed_paths", lambda *a, p=paths: list(p))
        monkeypatch.setattr(mg, "DRY_RUN", False)
        monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
        s = mg.Sweep()
        mg._evaluate(_loop_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": cfg}))
        assert len(_merges(writes)) == merged, files
        if not merged:
            assert "it edits .github/" in s.review[0][2]
