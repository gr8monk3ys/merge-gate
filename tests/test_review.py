"""The reviewed path: a PR outside the shape allowlist may merge when its repo
opted in and a reviewer passed the exact commit the gate would merge.

Pure, like test_trust.py: every GitHub read is a stand-in.
"""
import pytest

import classify_pr as cp
import gate_ledger as ledger
import merge_gate as mg
from test_trust import _gh, _merges, _repo, _run

REVIEW = f"commits/h7/statuses"


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
