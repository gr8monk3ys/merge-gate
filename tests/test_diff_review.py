"""The diff review: a repo may require that a PR merging on its allowlisted
shape also carries a passing `merge-gate/diff-review` status on its head.

Pure, like test_review.py: every GitHub read is a stand-in.
"""
import pytest

import classify_pr as cp
import gate_ledger as ledger
import merge_gate as mg
from test_review import _loop_pr
from test_trust import _bot_pr, _gh, _merges, _repo, _run

DIFF = "merge-gate/diff-review"


def _evaluate(monkeypatch, cfg, *, diff=None, review=None, paths=("uv.lock",),
              runs=None, pr=None):
    """Judge PR #7 on o/r in a real run; returns (sweep, writes, reads).

    `diff` / `review` answer the two status reads; None serves no answer, so
    a read the gate should not make fails the test."""
    runs = runs if runs is not None else [_run("ci", "success")]
    answers = []
    if diff is not None:
        answers.append((DIFF, diff))
    if review is not None:
        answers.append(("merge-gate/review", review))
    answers += _repo(["ci"], runs)
    reads, fake = [], _gh(answers)
    writes = []
    monkeypatch.setattr(mg, "sh_strict", lambda *a: reads.append(" ".join(a)) or fake(*a))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: list(paths))
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(pr or _bot_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": cfg}))
    return s, writes, reads


def test_the_opt_in_is_literal_true_only(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("repos:\n"
                 "  a: { require_diff_review: true }\n"
                 "  b: { require_diff_review: 'yes' }\n"
                 "  c: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["require_diff_review"] is True
    assert cfg["b"]["require_diff_review"] is False
    assert cfg["c"]["require_diff_review"] is False


def test_a_fleet_default_applies_and_a_repo_may_decline_it(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("defaults:\n  require_diff_review: true\n"
                 "repos:\n"
                 "  a: {}\n"
                 "  b: { require_diff_review: false }\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["require_diff_review"] is True
    assert cfg["b"]["require_diff_review"] is False


def test_no_other_field_reads_the_defaults(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("defaults:\n  allow_reviewed: true\n  allow_tests_only: true\n"
                 "repos:\n  a: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["allow_reviewed"] is False and cfg["a"]["allow_tests_only"] is False


def test_without_the_opt_in_a_shape_merges_and_no_diff_review_is_read(monkeypatch):
    s, writes, reads = _evaluate(monkeypatch, {})
    assert len(_merges(writes)) == 1
    assert not any(DIFF in r for r in reads)
    assert ledger.read()[0][0]["diff_review"] is None


def test_a_passing_diff_review_merges_the_head_it_names(monkeypatch):
    s, writes, reads = _evaluate(monkeypatch, {"require_diff_review": True}, diff="success")
    [merge] = _merges(writes)
    assert merge[merge.index("--match-head-commit") + 1] == "h7"
    assert any("commits/h7/statuses" in r and DIFF in r for r in reads)
    [row] = ledger.read()[0]
    assert (row["decision"], row["shape"], row["diff_review"]) == (
        "MERGED", "lockfile-only", "success")


@pytest.mark.parametrize("posted, said", [
    ("", "not posted"), ("failure", "failure"), ("error", "error"),
    ("pending", "pending")])
def test_without_a_passing_diff_review_a_green_shape_routes(monkeypatch, posted, said):
    s, writes, _ = _evaluate(monkeypatch, {"require_diff_review": True}, diff=posted)
    assert s.armed == [] and _merges(writes) == []
    assert s.review[0][2] == f"lockfile-only; green, but {DIFF} is {said} at h7"
    assert ledger.read()[0][0]["decision"] == "ROUTED"


def test_the_diff_review_does_not_replace_the_checks(monkeypatch):
    # No diff-review answer is served: a red PR's status is never read.
    s, writes, _ = _evaluate(monkeypatch, {"require_diff_review": True},
                             runs=[_run("ci", "failure")])
    assert _merges(writes) == []
    assert s.review[0][2] == "required check(s) red: ci"


def test_a_diff_review_that_cannot_be_read_is_not_an_answer(monkeypatch):
    def boom(joined):
        raise mg.ReadFailed("HTTP 502")
    with pytest.raises(mg.ReadFailed):
        _evaluate(monkeypatch, {"require_diff_review": True}, diff=boom)


def test_a_diff_review_never_stands_in_for_the_reviewed_path(monkeypatch):
    # A mixed PR in a repo with both opt-ins: the diff review passed, the
    # review was never posted. Only the review counts there.
    s, writes, reads = _evaluate(
        monkeypatch, {"allow_reviewed": True, "require_diff_review": True},
        diff="success", review="", paths=("src/x.py",), pr=_loop_pr())
    assert _merges(writes) == []
    assert s.review[0][2] == f"shape=mixed; green, but {mg.REVIEW_CONTEXT} is not posted at h7"
    assert not any(DIFF in r for r in reads)


def test_the_reviewed_path_does_not_also_owe_a_diff_review(monkeypatch):
    s, writes, reads = _evaluate(
        monkeypatch, {"allow_reviewed": True, "require_diff_review": True},
        review="success", paths=("src/x.py",), pr=_loop_pr())
    assert len(_merges(writes)) == 1
    assert not any(DIFF in r for r in reads)


def test_the_breakers_still_hold_a_diff_reviewed_pr(monkeypatch):
    s, writes, _ = _evaluate(monkeypatch, {"require_diff_review": True,
                                           "merge_gate_paused": True}, diff="success")
    assert _merges(writes) == [] and len(s.held) == 1


def test_a_dry_run_reports_the_same_routing(monkeypatch):
    monkeypatch.setattr(mg, "DRY_RUN", True)
    answers = [(DIFF, "")] + _repo(["ci"], [_run("ci", "success")])
    monkeypatch.setattr(mg, "sh_strict", _gh(answers))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    writes = []
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, mg.RepoCache({"r": {"require_diff_review": True}}))
    assert s.armed == [] and writes == []
    assert s.review[0][2] == f"lockfile-only; green, but {DIFF} is not posted at h7"
