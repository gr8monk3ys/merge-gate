"""Property tests for what stands between a diff and an auto-merge, other
than the evidence rule: the path classifier, the version rule, the
tests-only veto and the report parser.

Each property is stated against an oracle that does not use the gate's own
globs or regexes, so a pattern that matches more than it means to fails
here. CI runs these with the `release` profile (tests/conftest.py).
"""
import io
import json
import zipfile

from hypothesis import assume, given, strategies as st

import classify_pr as cp
import merge_gate as mg

# Paths the gate must never read as anything but source code.
SOURCE = st.builds(
    lambda dirs, base: "/".join(dirs + [base]),
    st.lists(st.sampled_from(["src", "lib", "app", "pkg", "internal", "cmd", "server"]),
             min_size=1, max_size=3),
    st.sampled_from(["main.py", "index.ts", "server.go", "utils.js", "app.rb", "mod.rs"]))
CONTENT = st.builds(
    lambda root, slug: f"{root}/{slug}.md",
    st.sampled_from(["content", "posts", "_posts", "blog", "app/blog", "site/content"]),
    st.text("abc-", min_size=1, max_size=6))
HARMLESS = st.sampled_from([
    "uv.lock", "package-lock.json", "web/yarn.lock", "go.sum",
    "package.json", "pyproject.toml", "requirements.txt", "api/requirements/dev.txt",
    "tests/test_a.py", "src/__tests__/b.test.ts", "tests/uv.lock", "test/helpers.py",
    ".github/workflows/ci.yml", "data/out.json", "public/data/feed.json",
])
DATA_DIRS = st.one_of(st.none(), st.lists(st.sampled_from(["data", "public/data"]),
                                          max_size=2))

PREFIX = st.sampled_from(["", "", "v", "^", "~", ">="])
TRIPLE = st.tuples(st.integers(0, 40), st.integers(0, 40), st.integers(0, 40))


def ver(prefix, triple):
    return prefix + ".".join(map(str, triple))


@st.composite
def delta(draw, breaking):
    """(old, new) version strings; `breaking` moves the major (or a 0.x minor)."""
    a = draw(TRIPLE)
    if breaking:
        b = draw(TRIPLE)
        assume(a[0] != b[0] or (a[0] == 0 and a[1] != b[1]))
    else:
        assume(a[0] > 0)
        b = (a[0], draw(st.integers(0, 40)), draw(st.integers(0, 40)))
    p = draw(PREFIX)
    return ver(p, a), ver(p, b)


@st.composite
def bump_body(draw, breaking):
    """A dependabot-style body. `breaking` plants one breaking delta among
    safe ones, at any position, in either format the parser reads."""
    deltas = draw(st.lists(delta(False), max_size=4))
    if breaking:
        deltas.insert(draw(st.integers(0, len(deltas))), draw(delta(True)))
    lines = []
    for i, (old, new) in enumerate(deltas):
        if draw(st.booleans()):
            lines.append(f"Updates `pkg{i}` from {old} to {new}")
        else:
            lines.append(f"| [pkg{i}](https://example.com/pkg{i}) | `{old}` | `{new}` |")
    return "\n".join(lines)


TITLE = st.sampled_from(["", "chore(deps): bump foo from 1.2.3 to 1.2.4",
                         "chore: refresh lockfile", "test: add cases"])
ANY_BODY = st.one_of(st.just(""), bump_body(False), bump_body(True))
DIFF = st.one_of(st.none(), st.just("+      - uses: actions/checkout@v5\n"))


def final_shape(paths, data_dirs, title, body, diff):
    return cp.promote_candidate(cp.classify(paths, data_dirs), title, body, diff)


@given(st.lists(st.one_of(HARMLESS, SOURCE, CONTENT), max_size=6), SOURCE,
       st.integers(0, 6), DATA_DIRS, TITLE, ANY_BODY, DIFF)
def test_a_diff_with_a_source_file_never_auto_merges(rest, src, at, dd, title, body, diff):
    paths = rest[:at] + [src] + rest[at:]
    assert not cp.auto_mergeable(final_shape(paths, dd, title, body, diff), paths)


@given(st.lists(st.one_of(HARMLESS, SOURCE), max_size=6), CONTENT,
       st.integers(0, 6), DATA_DIRS, TITLE, ANY_BODY, DIFF)
def test_a_diff_with_content_never_auto_merges(rest, post, at, dd, title, body, diff):
    paths = rest[:at] + [post] + rest[at:]
    assert not cp.auto_mergeable(final_shape(paths, dd, title, body, diff), paths)


DEP_PATHS = st.lists(st.sampled_from([
    "uv.lock", "package-lock.json", "web/yarn.lock", "package.json",
    "pyproject.toml", "requirements.txt", "api/requirements/dev.txt",
    ".github/workflows/ci.yml"]), min_size=1, max_size=4)


@given(DEP_PATHS, TITLE, bump_body(True), DIFF)
def test_one_breaking_delta_keeps_a_dependency_diff_from_auto_merging(paths, title, body, diff):
    assert cp.bump_kind(title, body) not in cp.AUTO_MERGE_BUMP_KINDS
    assert not cp.auto_mergeable(final_shape(paths, None, title, body, diff), paths)


@given(bump_body(False))
def test_safe_deltas_alone_are_read_as_safe(body):
    """Keeps the property above from passing because nothing parses."""
    assume(body)
    assert cp.bump_kind("", body) in cp.AUTO_MERGE_BUMP_KINDS


NEW_TEST = st.builds(
    lambda d, n: {"filename": f"{d}/test_{n}.py", "status": "added",
                  "patch": f"+def test_{n}():\n+    assert True\n"},
    st.sampled_from(["tests", "tests/unit", "pkg/tests"]), st.text("abcdef", min_size=1, max_size=5))
NOT_A_NEW_TEST = st.one_of(
    NEW_TEST.map(lambda f: dict(f, status="modified")),
    NEW_TEST.map(lambda f: dict(f, status="renamed", previous_filename="tests/test_old.py")),
    NEW_TEST.map(lambda f: dict(f, patch=None)),
    st.sampled_from(["tests/helpers.py", "tests/conftest.py", "tests/__init__.py",
                     "tests/uv.lock", "tests/data.json", "e2e/test_login.py",
                     "src/main.py"]).map(
        lambda p: {"filename": p, "status": "added", "patch": "+x = 1\n"}),
)


def unique_names(files):
    names = [f["filename"].rsplit("/", 1)[-1] for f in files]
    return len(names) == len(set(names))


@given(st.lists(NEW_TEST, max_size=4), NOT_A_NEW_TEST, st.integers(0, 4))
def test_anything_but_a_brand_new_test_module_vetoes_tests_only(good, bad, at):
    files = good[:at] + [bad] + good[at:]
    assert cp.tests_only_veto(files, ()) is not None


@given(st.lists(NEW_TEST, min_size=1, max_size=4))
def test_brand_new_test_modules_alone_are_not_vetoed(files):
    """Keeps the property above from passing because everything is vetoed."""
    assume(unique_names(files))
    assert cp.tests_only_veto(files, ()) is None


@given(st.lists(NEW_TEST, min_size=1, max_size=4), st.data())
def test_a_new_test_named_like_an_existing_file_is_vetoed(files, data):
    assume(unique_names(files))
    clash = data.draw(st.sampled_from(files))["filename"].rsplit("/", 1)[-1]
    assert cp.tests_only_veto(files, [f"legacy/{clash.upper()}"]) is not None


REPO, SHA = "Owner/repo", "a" * 40
JSON = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-3, 9), st.floats(allow_nan=False),
              st.text("ab", max_size=3)),
    lambda kids: st.one_of(st.lists(kids, max_size=3),
                           st.dictionaries(st.text("ab", max_size=2), kids, max_size=3)),
    max_leaves=6)
COUNT = st.one_of(st.integers(0, 6), JSON)
STAGE = st.one_of(JSON, st.fixed_dictionaries(
    {"stage": st.one_of(st.sampled_from(["unit", "e2e", ""]), JSON),
     "tests": COUNT, "passed": COUNT, "failed": COUNT, "skipped": COUNT},
    optional={"passed_ids": st.one_of(st.lists(st.sampled_from(["a", "b", "c", ""]), max_size=5),
                                      JSON)}))
REPORT = st.one_of(JSON, st.fixed_dictionaries(
    {"schema": st.one_of(st.just(1), st.just(True), st.just(1.0), JSON),
     "repo": st.one_of(st.sampled_from([REPO, REPO.lower(), "other/repo"]), JSON),
     "commit": st.one_of(st.sampled_from([SHA, SHA[:7], "b" * 40]), JSON),
     "stages": st.one_of(st.lists(STAGE, max_size=3), JSON)}))


def zipped(doc, name=mg.VERIFY_FILE, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        zf.writestr(name, json.dumps(doc))
    return buf.getvalue()


@given(REPORT)
def test_an_accepted_report_is_for_this_commit_and_adds_up(doc):
    stages, why = mg.parse_verify_report(zipped(doc), REPO, SHA)
    assert (stages is None) != (why is None)
    if stages is None:
        return
    assert doc["schema"] is not True and doc["schema"] == 1
    assert doc["repo"].lower() == REPO.lower() and doc["commit"] == SHA
    assert len(stages) == len(doc["stages"]) >= 1
    for c in stages.values():
        assert all(type(c[k]) is int and c[k] >= 0 for k in mg._COUNTS)
        assert c["passed"] + c["failed"] + c["skipped"] == c["tests"]
        ids = c.get("passed_ids", [])
        assert ids == sorted(set(ids)) and "" not in ids and len(ids) <= c["passed"]


GOOD = {"schema": 1, "repo": REPO, "commit": SHA,
        "stages": [{"stage": "unit", "tests": 3, "passed": 3, "failed": 0,
                    "skipped": 0, "passed_ids": ["a", "b", "c"]}]}


@given(st.sampled_from([zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]), st.data())
def test_a_damaged_artifact_is_refused_not_raised(method, data):
    """The artifact is bytes a PR's own CI run uploaded: any damage to them
    must come back as a reason, never as an exception that stops the sweep."""
    raw = bytearray(zipped(GOOD, method=method))
    for _ in range(data.draw(st.integers(1, 4))):
        raw[data.draw(st.integers(0, len(raw) - 1))] = data.draw(st.integers(0, 255))
    raw = bytes(raw[:data.draw(st.integers(0, len(raw)))]) if data.draw(st.booleans()) else bytes(raw)
    stages, why = mg.parse_verify_report(raw, REPO, SHA)
    assert (stages is None) != (why is None)


@given(st.binary(max_size=200))
def test_arbitrary_bytes_are_refused_not_raised(raw):
    assert mg.parse_verify_report(raw, REPO, SHA)[0] is None


def test_a_deeply_nested_report_is_refused_not_raised():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(mg.VERIFY_FILE, "[" * 100_000 + "]" * 100_000)
    assert mg.parse_verify_report(buf.getvalue(), REPO, SHA)[0] is None
