"""Trust hardening: what "green" means, what gets recorded, and when the
gate refuses to merge even a green PR.

Pure, like test_policy.py: every GitHub read is a stand-in.
"""
import json

import pytest

import classify_pr as cp
import merge_gate as mg


def _gh(answers):
    """A sh_strict stand-in: answer by the first key found in the argv."""
    def fake(*args):
        joined = " ".join(args)
        for key, val in answers:
            if key in joined:
                return val(joined) if callable(val) else val
        raise AssertionError(f"unexpected gh read: {joined}")
    return fake


def _run(name, conclusion, *, id=1, done="2026-11-01T00:00:00Z", app="github-actions"):
    return {"id": id, "name": name, "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "started_at": done, "completed_at":
            done if conclusion else None, "app": app}


def _repo(required, runs, branch_head="base0", base_runs=None):
    """A green-shaped PR #7 on o/r whose check runs are `runs`."""
    base_runs = base_runs if base_runs is not None else []
    return [
        ("pulls/7 --jq .head.sha", "h7\nmain\nfalse\nclean\nbase0\n"),
        (".auto_merge != null", "false"),
        (".allow_auto_merge", "true"),
        ("required_status_checks", json.dumps(required)),
        ("commits/h7/check-runs", json.dumps(runs)),
        (f"commits/{branch_head}/check-runs", json.dumps(base_runs)),
        ("branches/main --jq .commit.sha", branch_head),
    ]


# --- item 2: reading checks ----------------------------------------------

def test_check_runs_are_read_paginated_at_100(monkeypatch):
    seen = []

    def fake(*args):
        seen.append(args)
        # Two pages, as `--paginate --jq '[...]'` prints them: one array a line.
        return (json.dumps([_run("a", "success", id=1)]) + "\n"
                + json.dumps([_run("b", "success", id=2)]) + "\n")
    monkeypatch.setattr(mg, "sh_strict", fake)
    runs = mg.check_runs("o/r", "h7")
    assert [r["name"] for r in runs] == ["a", "b"]
    assert "--paginate" in seen[0]
    assert "per_page=100" in seen[0][1]


def test_a_garbled_check_runs_page_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: "[]\n<html>502</html>")
    with pytest.raises(mg.ReadFailed):
        mg.check_runs("o/r", "h7")


def test_the_newest_run_of_a_name_wins_whatever_order_github_lists():
    old_green = _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")
    new_red = _run("ci", "failure", id=2, done="2026-11-02T00:00:00Z")
    assert mg.latest_by_name([new_red, old_green])["ci"]["conclusion"] == "failure"
    assert mg.latest_by_name([old_green, new_red])["ci"]["conclusion"] == "failure"


def test_an_in_flight_rerun_shadows_the_finished_attempt_before_it():
    old_green = _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")
    rerun = dict(_run("ci", None, id=2), started_at="2026-11-02T00:00:00Z")
    assert mg.latest_by_name([rerun, old_green])["ci"]["conclusion"] is None


def test_the_id_breaks_a_timestamp_tie():
    a = _run("ci", "success", id=5)
    b = _run("ci", "failure", id=9)
    assert mg.latest_by_name([b, a])["ci"]["id"] == 9


def test_a_newer_red_rerun_blocks_a_pr_an_older_green_run_would_pass(monkeypatch):
    runs = [_run("ci", "failure", id=2, done="2026-11-02T00:00:00Z"),
            _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], runs)))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is False and "red: ci" in why


def test_a_skipped_required_check_is_not_green_by_default(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test"], [_run("test", "skipped")])))
    st = mg.read_state("o/r", 7)
    green, why = mg.judge(st)
    assert green is False
    assert "skipped" in why and "may_skip" in why and "test" in why
    assert st.green_by_skip is False


def test_a_skipped_check_the_repo_allowlists_passes_and_says_so(monkeypatch):
    runs = [_run("test", "skipped"), _run("lint", "success")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test", "lint"], runs)))
    st = mg.read_state("o/r", 7, cfg={"may_skip": ["test"]})
    green, why = mg.judge(st)
    assert green is True
    assert st.green_by_skip is True
    assert "green by skip: test" in why


def test_may_skip_is_per_check_not_per_repo(monkeypatch):
    runs = [_run("test", "skipped"), _run("lint", "skipped")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test", "lint"], runs)))
    st = mg.read_state("o/r", 7, cfg={"may_skip": ["test"]})
    green, why = mg.judge(st)
    assert green is False and why.endswith("may_skip: lint")


def test_a_required_check_still_running_is_not_green(monkeypatch):
    # conclusion null used to be neither "missing" nor "failed", so a check
    # that had not finished fell through as green.
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", None)])))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is False and "still running: ci" in why


def test_neutral_and_cancelled_are_not_green(monkeypatch):
    for concl in ("neutral", "cancelled", "timed_out", "action_required"):
        monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", concl)])))
        assert mg.judge(mg.read_state("o/r", 7))[0] is False


def test_each_required_check_is_recorded_with_its_conclusion_and_app(monkeypatch):
    runs = [_run("ci", "success", app="github-actions")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci", "e2e"], runs)))
    st = mg.read_state("o/r", 7)
    assert st.checks == [
        {"name": "ci", "conclusion": "success", "app": "github-actions"},
        {"name": "e2e", "conclusion": "missing", "app": None},
    ]


def test_repo_config_reaches_read_state_through_the_sweep_cache(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test"], [_run("test", "skipped")])))
    cache = mg.RepoCache({"r": {"may_skip": ["test"]}})
    assert mg.judge(mg.read_state("o/r", 7, cache))[0] is True


def test_gate_config_is_read_from_repos_yml(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("repos:\n"
                 "  a: { may_skip: [test], merge_gate_max_per_day: 1 }\n"
                 "  b: { merge_gate_paused: true, merge_gate_ack_reverts: [abc1234] }\n"
                 "  c: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["may_skip"] == ["test"]
    assert cfg["a"]["merge_gate_max_per_day"] == 1
    assert cfg["b"]["merge_gate_paused"] is True
    assert cfg["b"]["merge_gate_ack_reverts"] == ["abc1234"]
    # Absent is the strict value, never a permissive one.
    assert cfg["c"] == {"may_skip": [], "correctness_checks": [],
                        "merge_gate_max_per_day": None,
                        "merge_gate_paused": False, "merge_gate_ack_reverts": []}


def test_a_missing_repos_yml_yields_no_gate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_REPOS_YML", str(tmp_path / "nope.yml"))
    assert cp.load_gate_config() == {}
