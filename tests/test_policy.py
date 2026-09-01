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
])
def test_delta_kind(old, new, kind):
    assert cp.delta_kind(old, new) == kind


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
