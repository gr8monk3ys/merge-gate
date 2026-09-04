"""The merge policy, as executable statements.

Every test here is a pure function of its inputs: no network, no `gh`. Each
one encodes a rule that was learned from a real PR that merged when it should
not have, or was blocked when it should not have been -- DECISIONS.md tells
those stories. The live fixtures (`classify_pr.py --validate`) belong to
whoever operates the gate.
"""
import importlib

import pytest

import classify_pr as cp
import merge_gate as mg


def test_builtin_selftests_pass(capsys):
    assert cp.selftest() == 0


# --- shape is a function of changed paths, and nothing else -----------------

@pytest.mark.parametrize("paths,shape", [
    (["bun.lockb"], "lockfile-only"),
    (["uv.lock"], "lockfile-only"),
    (["src/a.test.ts", "src/b.test.ts"], "tests-only"),
    (["src/a.test.ts", "src/main.ts"], "mixed"),
    (["content/posts/x.md"], "content"),
    (["requirements.txt"], "dependency-bump-candidate"),
    (["pyproject.toml", "uv.lock"], "dependency-bump-candidate"),
    ([".github/workflows/ci.yml"], "ci-fix-candidate"),
    ([], "empty"),
    (None, "unreadable"),
])
def test_shape(paths, shape):
    assert cp.classify(paths) == shape


def test_there_is_no_partial_credit():
    # Forty test files and one source file is not "mostly tests".
    assert cp.classify(["tests/a_test.py"] * 40 + ["src/x.py"]) == "mixed"


def test_data_dir_is_opt_in_and_absence_means_deny():
    assert cp.classify(["data/out.csv"], ["data"]) == "data-artifact-only"
    assert cp.classify(["data/out.csv"], None) == "mixed"


def test_requirements_txt_is_a_manifest_not_a_lockfile():
    # Counting it as a lockfile let pytest 7->9 and cryptography 48->50 merge
    # with the version rule never consulted.
    assert not cp.is_lockfile("requirements.txt")
    assert cp.is_manifest("requirements-dev.txt")
    assert cp.is_manifest("requirements/base.txt")


# --- the content veto is independent of shape, and absolute -----------------

def test_content_veto_blocks_regardless_of_shape():
    paths = ["tests/x_test.py", "content/posts/x.md"]
    assert cp.content_veto(paths)
    assert not cp.auto_mergeable(cp.classify(paths), paths)


def test_unreadable_diff_vetoes():
    # "I could not check for content" must never resolve to "no content".
    assert cp.content_veto(None) is True
    assert cp.auto_mergeable("unreadable", None) is False


# --- version deltas ---------------------------------------------------------

@pytest.mark.parametrize("old,new,kind", [
    ("3.1.2", "3.1.5", "patch"),
    ("6.2.0", "6.4.0", "minor"),
    ("4.3.1", "7.0.1", "major"),
    ("5", "7", "major"),
    ("0.27.0", "0.41.0", "major"),      # under 0.x the minor position breaks
    ("0.27.7", "0.27.9", "patch"),
    ("0.52.1", "1.3.1", "major"),
    ("c3400c2f38909e0dcf3c3a41f2030a8217be5d3e",
     "c4dd10e44af883a891fe31ced449bcb4a6728b9b", "sha"),
    ("<8.0.0,>=7.4.4", ">=9", "unknown"),  # a range is not a version
    # A lone lower bound is a floor, and the floor is what moved.
    (">=1.2.2", ">=1.2.3", "patch"),
    (">=0.52.3", ">=0.52.4", "patch"),      # 0.x patch is still a patch
    (">=0.16.3", ">=0.16.5", "patch"),
    (">=0.27.0", ">=0.41.0", "major"),      # 0.x minor is still breaking
    (">=0.122.0", ">=1.2.0", "major"),
    (">=3.15", ">=3.19", "minor"),
    (">=6.1.1", ">=6.1.2", "patch"),
    ("~=1.2.2", "~=1.2.3", "patch"),
    ("==1.2.2", "==1.2.3", "patch"),
    ("==1.2.2", "==2.0.0", "major"),
    (">=7.4.4,<8", ">=9", "unknown"),       # a compound range is still not a version
    (">=7.4.4", ">=9,<10", "unknown"),
    ("<8.0.0", "<9.0.0", "unknown"),        # a ceiling alone says nothing about the floor
])
def test_delta_kind(old, new, kind):
    assert cp.delta_kind(old, new) == kind


# --- requirement-range bumps (pyproject / requirements.txt) ----------------
#
# Dependabot's pip "requirement" PRs say only "Updates the requirements on
# [pkg](url) to permit the latest version" in the body -- no versions at all.
# The title carries the bounds. See DECISIONS.md section 30.

_BODY_REQUIREMENT = (
    "Updates the requirements on [python-dotenv]"
    "(https://github.com/theskumar/python-dotenv) to permit the latest "
    "version.\n<details>\n<summary>Release notes</summary>\n</details>\n")


@pytest.mark.parametrize("title,kind", [
    ("chore(deps): update python-dotenv requirement from >=1.2.2 to >=1.2.3",
     "patch"),
    ("chore(deps): update uvicorn requirement from >=0.52.3 to >=0.52.4",
     "patch"),
    ("chore(deps): update anthropic requirement from >=0.122.0 to >=1.2.0",
     "major"),
    ("chore(deps-dev): update openai requirement from >=3.1.0 to >=3.6.0",
     "minor"),
    ("chore(deps): Update idna requirement from >=3.15 to >=3.19", "minor"),
    ("Update pytest requirement from <8.0.0,>=7.4.4 to >=9", "unknown"),
])
def test_requirement_range_bump_reads_the_floor(title, kind):
    assert cp.bump_kind(title, _BODY_REQUIREMENT) == kind


def test_requirement_range_promotes_by_floor_delta():
    body = _BODY_REQUIREMENT
    patch = "chore(deps): update python-dotenv requirement from >=1.2.2 to >=1.2.3"
    major = "chore(deps): update anthropic requirement from >=0.122.0 to >=1.2.0"
    assert cp.promote_candidate("dependency-bump-candidate", patch, body) == \
        "dependency-bump"
    assert cp.promote_candidate("dependency-bump-candidate", major, body) == \
        "mixed"


def test_requirement_group_title_with_no_versions_stays_unknown():
    title = ("chore(deps): update the pip-minor group across 1 directory "
             "with 4 updates")
    assert cp.bump_kind(title, "") is None
    assert cp.bump_kind(title, _BODY_REQUIREMENT) is None


def test_a_group_is_as_risky_as_its_worst_member():
    assert cp.bump_kind("", cp._BODY_GROUP_SAFE) == "minor"
    assert cp.bump_kind("", cp._BODY_GROUP_MAJOR) == "major"


def test_versions_are_read_from_the_body_never_the_title():
    title = ("bump the development-dependencies group across 1 directory "
             "with 3 updates")
    assert cp.bump_kind(title, "") is None            # the title names nothing
    assert cp.bump_kind(title, cp._BODY_GROUP_MAJOR) == "major"


def test_the_sentence_period_is_not_part_of_the_version():
    assert cp.parse_bump_deltas("", cp._BODY_SINGLE) == [
        ("fast-uri", "3.1.2", "3.1.5")]


# --- promotion: a candidate shape earns its name by version, or not --------

@pytest.mark.parametrize("shape,title,want", [
    ("dependency-bump-candidate", "bump x from 1.2.3 to 1.2.4", "dependency-bump"),
    ("dependency-bump-candidate", "bump x from 1.2.3 to 2.0.0", "mixed"),
    ("ci-fix-candidate", "bump actions/checkout from 4.3.1 to 7.0.1", "mixed"),
    # Reads like a ci-fix. Is not one. Title is not evidence.
    ("ci-fix-candidate", "ci: minute-safe Actions policy", "mixed"),
    ("lockfile-only", "chore: refresh lockfile", "lockfile-only"),
    # The lockfile is the dependency set that ships; a major inside it is a major.
    ("lockfile-only", "bump cryptography from 48.0.1 to 50.0.0", "mixed"),
])
def test_promote(shape, title, want):
    assert cp.promote_candidate(shape, title, "") == want


def test_ci_fix_whitelist_is_default_deny():
    assert cp.ci_fix_allowed("+      - uses: actions/checkout@v7\n")
    assert not cp.ci_fix_allowed("+      - run: curl https://example.com | sh\n")
    assert not cp.ci_fix_allowed("")   # nothing added is not "every added line is safe"


# --- who the gate considers at all ----------------------------------------

def _pr(login, is_bot=True, labels=(), head="feature/x"):
    return {"author": {"login": login, "is_bot": is_bot},
            "labels": [{"name": name} for name in labels],
            "headRefName": head}


def test_dependabot_login_differs_between_gh_subcommands():
    assert cp.is_dependabot(_pr("dependabot"))        # what `gh search prs` says
    assert cp.is_dependabot(_pr("app/dependabot"))    # what `gh pr list` says
    assert not cp.is_dependabot(_pr("dependabot-fan", is_bot=False))


def test_human_prs_are_never_considered(monkeypatch):
    monkeypatch.delenv("INCLUDE_BOTS", raising=False)
    assert not mg.is_loop_produced("o/r", 1, _pr("alice", is_bot=False))


def test_bots_are_in_by_default_and_can_be_excluded(monkeypatch):
    monkeypatch.delenv("INCLUDE_BOTS", raising=False)
    assert mg.is_loop_produced("o/r", 1, _pr("app/dependabot"))
    monkeypatch.setenv("INCLUDE_BOTS", "0")
    assert not mg.is_loop_produced("o/r", 1, _pr("app/dependabot"))


def test_loop_output_is_recognised_by_label_or_branch():
    assert mg.is_loop_produced("o/r", 1, _pr("alice", False, labels=["automated"]))
    assert mg.is_loop_produced("o/r", 1, _pr("alice", False,
                                             head="auto/dep-hygiene-20260801"))


# --- conflicts --------------------------------------------------------------

@pytest.mark.parametrize("state,conflict", [
    ("dirty", True), ("clean", False), ("unknown", False), ("", False), (None, False),
])
def test_only_dirty_is_a_conflict_verdict(state, conflict):
    # "unknown" means GitHub has not answered yet, and no answer is not an answer.
    assert cp.is_conflict(state) is conflict


# --- reading GitHub: the absence of an answer is never rendered as one -------

def test_required_check_names_may_contain_commas(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict",
                        lambda *a: '["Lint, Test & Build", "quality"]')
    assert mg.required_checks("o/r", "main") == ["Lint, Test & Build", "quality"]


def test_an_unprotected_branch_is_a_real_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: None)       # GitHub said 404
    assert mg.required_checks("o/r", "main") == []


def test_an_unparseable_body_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: "<html>502</html>")
    with pytest.raises(mg.ReadFailed):
        mg.required_checks("o/r", "main")


def test_sh_strict_separates_no_from_silence(monkeypatch):
    monkeypatch.setattr(mg, "sh", lambda *a: (1, "", "HTTP 404: Branch not protected"))
    assert mg.sh_strict("api", "x") is None
    monkeypatch.setattr(mg, "sh", lambda *a: (1, "", "tls: failed to verify certificate"))
    with pytest.raises(mg.ReadFailed):
        mg.sh_strict("api", "x")


# --- owners -----------------------------------------------------------------

def test_owners_come_from_env_and_an_empty_fleet_is_refused(monkeypatch):
    monkeypatch.delenv("GATE_OWNERS", raising=False)
    try:
        m = importlib.reload(cp)
        assert m.OWNERS == []
        with pytest.raises(SystemExit):
            m.require_owners()
        monkeypatch.setenv("GATE_OWNERS", "alice, acme-org")
        m = importlib.reload(cp)
        assert m.OWNERS == ["alice", "acme-org"]
    finally:
        monkeypatch.delenv("GATE_OWNERS", raising=False)
        importlib.reload(cp)


# ---------------------------------------------------------------------------
# repos.yml resolution
#
# This is policy input, not plumbing: `data_dir` opt-ins are the only thing
# that makes `data-artifact-only` match anything, and load_data_dirs()
# swallows a missing file. Resolve the path wrong and the shape quietly stops
# matching, with nothing in any report to say so. It resolved wrong for every
# installed copy of this package until 0.2.0.


def test_repos_yml_is_explicit_then_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_REPOS_YML", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cp.repos_yml_path() == str(tmp_path / "repos.yml")

    monkeypatch.setenv("GATE_REPOS_YML", "/somewhere/else/repos.yml")
    assert cp.repos_yml_path() == "/somewhere/else/repos.yml"


def test_repos_yml_env_is_read_per_call_not_at_import(tmp_path, monkeypatch):
    """Set late, honoured anyway.

    A consumer that binds the package's config after importing it -- which is
    what a control plane's own shim does -- would otherwise get the value
    frozen at import time and never know.
    """
    f = tmp_path / "repos.yml"
    f.write_text("repos:\n  demo: {loops: [x], data_dir: [data/]}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(f))
    assert cp.parse_repos_yml()["demo"]["data_dir"] == ["data/"]
    assert cp.load_data_dirs() == {"demo": ["data/"]}


def test_missing_repos_yml_degrades_only_for_data_dirs(tmp_path, monkeypatch):
    """parse_repos_yml raises; load_data_dirs returns {}. Both deliberate."""
    monkeypatch.setenv("GATE_REPOS_YML", str(tmp_path / "nope.yml"))
    with pytest.raises(OSError):
        cp.parse_repos_yml()
    assert cp.load_data_dirs() == {}


# --- base freshness ---------------------------------------------------------
#
# finance-owl, 2026-09-02: the gate merged #148 (sentry ^10.72.0 into
# packages/backend/package.json) and, 21 seconds later, #145 -- a
# lockfile-only PR whose green checks had run against the PREVIOUS main and
# whose lockfile still said ^10.70.0. main went red with
# ERR_PNPM_OUTDATED_LOCKFILE. Branch protection had strict:false, as in 57 of
# 58 repos, so GitHub never re-ran anything on the moved base. The head rule
# (--match-head-commit) had no counterpart for the base.

def _gh(answers):
    """A sh_strict stand-in: answer by the first key found in the argv."""
    def fake(*args):
        joined = " ".join(args)
        for key, val in answers:
            if key in joined:
                return val
        raise AssertionError(f"unexpected gh read: {joined}")
    return fake


def _green_repo(branch_head, base_sha="base0"):
    return [
        ("pulls/7 --jq .head.sha", f"h7\nmain\nfalse\nclean\n{base_sha}\n"),
        (".auto_merge != null", "false"),
        (".allow_auto_merge", "true"),
        ("required_status_checks", '["ci"]'),
        ("check-runs", '[{"name":"ci","conclusion":"success"}]'),
        ("branches/main --jq .commit.sha", branch_head),
    ]


def test_mergeable_state_clean_is_not_evidence_the_base_is_fresh(monkeypatch):
    # trading-bot#107, read 2026-09-01: mergeable_state=clean while base.sha
    # sat two commits behind main. With strict:false GitHub never says
    # "behind"; the only honest signal is base.sha against the branch head.
    monkeypatch.setattr(mg, "sh_strict", _gh(_green_repo("base1")))
    green, why, sha, base = mg.check_state("o/r", 7)
    assert green is False
    assert why.startswith(mg.STALE_BASE)
    assert "base0" in why and "base1" in why
    assert (sha, base) == ("h7", "main")


def test_a_fresh_base_merges(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_green_repo("base0")))
    assert mg.check_state("o/r", 7)[:2] == (True, "green: ci")


def test_a_merge_this_sweep_stales_every_sibling_without_asking_github(monkeypatch):
    # After the gate moves main, every other candidate in the repo is stale by
    # construction. That verdict must not depend on a ref read racing the
    # merge that just happened.
    answers = [a for a in _green_repo("base0") if "branches/" not in a[0]]
    monkeypatch.setattr(mg, "sh_strict", _gh(answers))   # branch read would raise
    green, why, _, _ = mg.check_state("o/r", 7, moved={("o/r", "main")})
    assert green is False
    assert why.startswith(mg.STALE_BASE)


def _bot_pr(num):
    return {"number": num, "title": "bump x from 1.0.0 to 1.0.1", "body": "",
            "author": {"login": "app/dependabot", "is_bot": True},
            "labels": [], "headRefName": f"dependabot/npm_and_yarn/x-{num}"}


def test_the_second_green_pr_in_a_repo_waits_for_the_next_sweep(monkeypatch):
    # This is exactly how #145 landed 21s after #148: both green, both judged
    # against the same pre-sweep main, both merged. One merge per base per
    # sweep; the rest get a rebase request and a section of their own.
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "sh_strict", _gh(_green_repo("base0")))
    monkeypatch.setattr(mg, "request_rebase", lambda *a: " — would request dependabot rebase")
    s = mg.Sweep()
    mg._evaluate(_bot_pr(7), "o/r", 7, {}, s)
    mg._evaluate(_bot_pr(7), "o/r", 7, {}, s)
    assert [r[1] for r in s.armed] == [7]
    assert len(s.stale) == 1 and s.review == []
    assert s.stale[0][2].startswith(mg.STALE_BASE)
    assert s.stale[0][2].endswith("would request dependabot rebase")
    assert ("o/r", "main") in s.moved


def test_a_stale_loop_pr_routes_to_review_not_to_dependabot(monkeypatch):
    # #145 was a human's PR carrying the `automated` label. Nothing can rebase
    # it but its author, so it is a review item, not a rebase request.
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "sh_strict", _gh(_green_repo("base1")))
    monkeypatch.setattr(mg, "request_rebase",
                        lambda *a: pytest.fail("asked dependabot to rebase a loop PR"))
    pr = {"number": 7, "title": "chore: refresh lockfile", "body": "",
          "author": {"login": "alice", "is_bot": False},
          "labels": [{"name": "automated"}], "headRefName": "fix/lockfile"}
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 7, {}, s)
    assert s.armed == [] and s.stale == []
    assert len(s.review) == 1 and s.review[0][2].startswith(mg.STALE_BASE)


# --------------------------------------------------------------------------
# sweep() -- the queue has one author
#
# Everything that wants to know what the gate thinks used to rebuild this walk
# from the primitives. Two such rebuilds drifted: they predated the
# base-freshness rule, so a dashboard showed PRs as ready that the gate would
# refuse to merge that sweep.


def _queued(repo, number, title="t"):
    return {"repository": {"nameWithOwner": repo}, "number": number,
            "title": title, "body": ""}


def test_sweep_returns_the_verdicts_and_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(mg, "fetch_open_prs",
                        lambda: [_queued("o/a", 1), _queued("o/b", 2)])
    monkeypatch.setattr(mg, "load_data_dirs", lambda: {})
    monkeypatch.setattr(mg, "is_loop_produced", lambda repo, num, pr: True)

    def fake_evaluate(pr, repo, num, data_dirs, s):
        s.armed.append((repo, num, "why", pr["title"]))

    monkeypatch.setattr(mg, "_evaluate", fake_evaluate)

    s = mg.sweep()
    assert [(r, n) for r, n, _, _ in s.armed] == [("o/a", 1), ("o/b", 2)]
    assert capsys.readouterr().out == ""


def test_sweep_files_unread_prs_separately_from_decided_ones(monkeypatch):
    """A PR GitHub never answered for has no verdict, and must not borrow the
    wording of one -- that is how a partial sweep reads as a drained queue."""
    monkeypatch.setattr(mg, "fetch_open_prs", lambda: [_queued("o/a", 1)])
    monkeypatch.setattr(mg, "load_data_dirs", lambda: {})
    monkeypatch.setattr(mg, "is_loop_produced", lambda repo, num, pr: True)

    def boom(pr, repo, num, data_dirs, s):
        raise mg.ReadFailed("GitHub did not answer")

    monkeypatch.setattr(mg, "_evaluate", boom)

    s = mg.sweep()
    assert s.armed == [] and s.review == []
    assert len(s.failed) == 1


def test_sweep_skips_prs_that_are_not_loop_produced(monkeypatch):
    monkeypatch.setattr(mg, "fetch_open_prs", lambda: [_queued("o/a", 1)])
    monkeypatch.setattr(mg, "load_data_dirs", lambda: {})
    monkeypatch.setattr(mg, "is_loop_produced", lambda repo, num, pr: False)
    monkeypatch.setattr(mg, "_evaluate",
                        lambda *a: pytest.fail("evaluated a foreign PR"))
    s = mg.sweep()
    assert (s.armed, s.review, s.failed) == ([], [], [])
