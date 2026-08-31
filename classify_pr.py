#!/usr/bin/env python3
"""Classify a PR by the shape of its diff — the shared core of Phases 2-4.

Classification is computed from the PR's ACTUAL changed file paths. Never from
its title, its branch name, or the loop that claims to have produced it: a loop
that mislabels its own output would otherwise talk its way past the gate.

The governing rule: if ANY changed path falls outside the matched shape's
patterns, the shape does not match and the PR is `mixed`. `mixed` always routes
to human review. There is no partial credit — a PR that adds a test AND edits
source is not "mostly tests".

Usage:
    GATE_OWNERS=you,your-org python3 classify_pr.py   # classify all open PRs
    python3 classify_pr.py --validate                  # self-tests + live fixtures
    python3 classify_pr.py --json                      # machine-readable output
"""

import fnmatch
import json
import os
import re
import sys
import time

# Installed as a top-level module beside this one. Every gh call in the
# package goes through it, so an environment with no usable `gh` binary --
# a cloud runner, a container -- still gets a real answer instead of a
# sweep that silently examined nothing. See gh_transport.__doc__.
import gh_transport

# Every account the fleet owns code under, from GATE_OWNERS="user,org,...".
#
# A list, never a single string. The fleet this was built for spans a personal
# account AND an org, and a single owner string meant `gh repo list $OWNER`
# never returned the org's repos -- so they were not DECLINED by the merge
# policy, they were invisible to it, and the gate reported a drained queue
# that never included them. verify-gates.py had the identical bug against the
# identical org. Resolved at import so every script sees one list; consumers
# that need a non-empty fleet call require_owners() and fail loudly.
OWNERS = [o.strip() for o in os.environ.get("GATE_OWNERS", "").split(",")
          if o.strip()]


def require_owners():
    """The configured owners, or a clear exit -- never an empty sweep.

    An empty OWNERS would make all_repos() enumerate nothing and every
    report print a clean, complete-looking zero. That is the exact failure
    this codebase exists to prevent, so it is refused at the door.
    """
    if not OWNERS:
        raise SystemExit('set GATE_OWNERS="<user>[,<org>...]" -- the accounts '
                         "whose repos the gate should enumerate")
    return OWNERS

LOCKFILES = {
    "package-lock.json", "bun.lockb", "bun.lock", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock",
    "Gemfile.lock", "composer.lock", "go.sum",
}

TEST_GLOBS = [
    "*.test.*", "*.spec.*", "tests/*", "tests/**", "test/*", "test/**",
    "__tests__/*", "__tests__/**", "**/tests/**", "**/__tests__/**",
    "**/*.test.*", "**/*.spec.*", "test_*.py", "**/test_*.py",
    "*_test.py", "**/*_test.py", "conftest.py", "**/conftest.py",
    "e2e/**", "**/e2e/**", "cypress/**", "playwright/**",
]

# Content lives in a lot of shapes across this fleet; match the containers,
# not the extension, so a .mdx component in src/ is not mistaken for a post.
CONTENT_GLOBS = [
    "content/**", "**/content/**", "posts/**", "**/posts/**",
    "_posts/**", "**/_posts/**", "blog/**", "**/blog/**",
    "src/content/**", "app/blog/**", "data/posts/**",
]

CI_GLOBS = [".github/workflows/*", ".github/workflows/**"]

# ci-fix is DEFAULT DENY. A diff only qualifies if it matches one of these
# explicitly reviewed, known-safe repair patterns. Adding to this list is a
# deliberate act, never an inference.
CI_FIX_WHITELIST = [
    r"actions/(checkout|setup-node|setup-python|cache|upload-artifact|download-artifact)@v?\d+",
    r"astral-sh/setup-uv@v?\d+",
]

# Dependency manifests — the file a human edits to declare a dependency, as
# opposed to the lockfile a tool regenerates from it. The distinction is why a
# package.json+lockfile bump is not `lockfile-only`: one of those two files is
# a real decision.
MANIFESTS = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "go.mod", "Cargo.toml", "Gemfile", "composer.json",
    "build.gradle", "build.gradle.kts", "pom.xml", "pubspec.yaml",
}

ALLOWLIST = {"lockfile-only", "tests-only", "data-artifact-only", "ci-fix",
             "dependency-bump"}

# ---------------------------------------------------------------------------
# Dependency-bump policy. These three constants ARE the policy; everything
# below them is mechanism. Changing what auto-merges should be a one-line
# edit here, not a hunt through the parser.
# ---------------------------------------------------------------------------

# Which version deltas may auto-merge. Majors carry breaking changes by
# definition, and several gates in this fleet are lint-only — green there does
# not mean "still works".
AUTO_MERGE_BUMP_KINDS = {"patch", "minor"}

# Under semver, 0.x has no stability guarantee: 0.27 -> 0.41 may break exactly
# as hard as 1.0 -> 2.0. Treating a 0.x minor as major is the strict reading,
# and it is the right default when the gate downstream may be a linter.
ZERO_MAJOR_MINOR_IS_BREAKING = True

# SHA-pinned action bumps (github/codeql-action from c3400c2f… to c4dd10e4…)
# carry NO version information whatsoever — there is nothing to compare, so
# "is this patch or minor?" is unanswerable rather than false. Unanswerable
# routes to a human. Set to True to trust SHA bumps of already-pinned actions.
TRUST_SHA_PINNED_ACTION_BUMPS = False


def _match(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def is_lockfile(p):
    """A tool-REGENERATED resolution artifact, not a human declaration.

    requirements*.txt used to be counted here and is not one. It is where a
    Python dependency is DECLARED, so treating it as a lockfile let every
    Python bump skip the version rule: pytest 7.4.4 -> 9.1.1, mypy 1.8 -> 2.2,
    kafka-python 2.0.2 -> 3.0.8 and cryptography 48 -> 50 all classified as
    `lockfile-only` and were eligible to auto-merge with no version check at
    all. It now lives in MANIFESTS, so patch/minor still merge and majors
    route to a human — the same rule every other ecosystem gets.
    """
    return p.rsplit("/", 1)[-1] in LOCKFILES


def is_test(p):
    return _match(p, TEST_GLOBS)


def is_content(p):
    return _match(p, CONTENT_GLOBS)


def is_ci(p):
    return _match(p, CI_GLOBS)


def is_manifest(p):
    base = p.rsplit("/", 1)[-1]
    if base in MANIFESTS or fnmatch.fnmatch(base, "requirements*.txt"):
        return True
    # requirements/base.txt, requirements/dev.txt: the pip-tools layout keeps
    # the manifests in a directory, so the basename alone never matches. It
    # failed closed (`mixed`, a human), but the comment above this line had
    # promised the layout was covered for weeks while it was not.
    return fnmatch.fnmatch(p, "requirements/*.txt") or \
        fnmatch.fnmatch(p, "*/requirements/*.txt")


# Dependabot states every version delta in the PR body, in one of three
# formats. Parsing the BODY rather than the title matters: a title like
# "bump the development-dependencies group across 1 directory with 21 updates"
# names no versions at all, while its body tabulates all 21 — including the
# two majors (jest-dom 6->7, jsdom 28->30) that must block the whole group.
_RE_TABLE_ROW = re.compile(
    r'^\|\s*\[?([^\]|]+?)\]?(?:\([^)]*\))?\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|',
    re.M)
_RE_FROM_TO = re.compile(
    r'(?:Bumps|Updates)\s+\[?`?([^\s`\]]+)`?\]?(?:\([^)]*\))?\s+'
    # Greedy, then strip the sentence-ending period in code. A lazy match here
    # read "from 3.1.2 to 3.1.5." as a bump to version "3" — the `.` of the
    # sentence terminated the capture — turning a patch into a fake minor.
    r'from\s+([^\s,;]+?)\s+to\s+(\S+)', re.I)
# Titles must keep commas: pip ranges look like "from <8.0.0,>=7.4.4 to >=9",
# and splitting on the comma silently loses the constraint.
_RE_TITLE_FROM_TO = re.compile(r'\bfrom\s+(\S+)\s+to\s+(\S+)', re.I)
_RE_SHA = re.compile(r'^[0-9a-f]{7,40}$', re.I)


def parse_bump_deltas(title="", body=""):
    """Every (name, old, new) version pair a dependabot PR declares.

    Returns [] when the PR is not a dependency bump at all.
    """
    out = []
    for m in _RE_TABLE_ROW.finditer(body or ""):
        name, old, new = (x.strip() for x in m.groups())
        # Skip the markdown header separator row (| --- | --- | --- |).
        if set(old) <= set("- ") or set(new) <= set("- "):
            continue
        out.append((name, old, new))
    for m in _RE_FROM_TO.finditer(body or ""):
        out.append(tuple(x.strip().rstrip(".") for x in m.groups()))
    if not out:
        m = _RE_TITLE_FROM_TO.search(title or "")
        if m:
            out.append(("", m.group(1).strip(), m.group(2).strip()))
    # Dedupe while preserving order; group bodies repeat a package in both the
    # summary table and its per-package "Updates x from a to b" line.
    seen, uniq = set(), []
    for row in out:
        if row[1:] not in seen:
            seen.add(row[1:])
            uniq.append(row)
    return uniq


def _semver(s):
    """Leading numeric version triple, or None if there isn't one.

    Tolerates `v7`, `7`, `7.0`, `1.2.3-rc1`, `^1.2.3`, `~1.2.3`. Returns None
    for a git SHA or a version RANGE.

    Ranges must not be coerced. `>=9` would otherwise read as exactly 9.0.0
    and `<8.0.0,>=7.4.4` as 8.0.0, so "Update pytest requirement from
    <8.0.0,>=7.4.4 to >=9" would be scored as a confident 8->9 major when
    what actually changed is a constraint, not a pin. Scoring a constraint
    change as a version delta is a guess wearing a number.
    """
    s = (s or "").strip()
    if re.search(r'[<>,|*\s]', s):
        return None
    m = re.match(r'^[\^~=v]*(\d+)(?:\.(\d+))?(?:\.(\d+))?', s)
    if not m:
        return None
    return tuple(int(x) if x else 0 for x in m.groups())


def delta_kind(old, new):
    """Severity of a single version delta: patch | minor | major | unknown."""
    if _RE_SHA.match(old or "") and _RE_SHA.match(new or ""):
        return "sha"
    a, b = _semver(old), _semver(new)
    if a is None or b is None:
        return "unknown"
    if a[0] != b[0]:
        return "major"
    # 0.x: the minor position carries the breaking changes.
    if a[0] == 0 and ZERO_MAJOR_MINOR_IS_BREAKING and a[1] != b[1]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    if a[2] != b[2]:
        return "patch"
    return "patch"


_SEVERITY = {"patch": 0, "minor": 1, "sha": 2, "unknown": 3, "major": 4}


def bump_kind(title="", body=""):
    """Worst-case severity across every delta in the PR, or None if not a bump.

    A group bump is exactly as risky as the riskiest package inside it, so the
    maximum — never the average and never the first — is the honest answer.
    """
    deltas = parse_bump_deltas(title, body)
    if not deltas:
        return None
    kinds = [delta_kind(o, n) for _, o, n in deltas]
    if TRUST_SHA_PINNED_ACTION_BUMPS:
        kinds = ["patch" if k == "sha" else k for k in kinds]
    return max(kinds, key=lambda k: _SEVERITY[k])


def promote_candidate(shape, title="", body="", diff_text=None):
    """Resolve a `*-candidate` shape into its final, allowlist-eligible name.

    classify() is a pure function of file paths, so it can only ever say
    "these paths COULD be a safe shape". Deciding whether they actually are
    needs the PR's version metadata, which is why the candidate shapes exist.

    This step was missing entirely: `ci-fix` sat in ALLOWLIST while classify()
    only ever returned `ci-fix-candidate`, and ci_fix_allowed() — the function
    that bridges them — had no callers anywhere in the repo. The whitelist
    documented in DECISIONS.md had therefore never let a single PR through.
    """
    kind = bump_kind(title, body)

    # A lockfile-only diff still ships a version change. One PR touched
    # nothing but uv.lock -- so `lockfile-only` is the honest shape -- while
    # moving cryptography from 48.0.1 to 50.0.0, and it armed for auto-merge
    # with the version rule never consulted. Whether the manifest changed
    # describes how the range was WRITTEN, not how far the code MOVED.
    #
    # Only demote when there is metadata to demote on: a loop-produced
    # "chore: refresh lockfile" carries no from/to at all, so bump_kind() is
    # None and its long-standing behaviour is untouched.
    if shape == "lockfile-only":
        if kind is not None and kind not in AUTO_MERGE_BUMP_KINDS:
            return "mixed"
        return shape

    if shape not in ("dependency-bump-candidate", "ci-fix-candidate"):
        return shape

    if kind is not None:
        return "dependency-bump" if kind in AUTO_MERGE_BUMP_KINDS else "mixed"

    # No bump metadata: not dependabot's work. A hand-written CI repair can
    # still qualify via the explicitly reviewed action whitelist, which stays
    # default-deny — a diff we cannot read never qualifies.
    if shape == "ci-fix-candidate" and diff_text and ci_fix_allowed(diff_text):
        return "ci-fix"
    return "mixed"


def classify(paths, data_dirs=None):
    """Return the diff shape for a list of changed paths.

    data_dirs: paths a repo has explicitly opted into via repos.yml `data_dir`.
    Absence means DENY — an unset field can never yield data-artifact-only.
    """
    # None is "the API could not be read", NOT "the diff is empty". Keep them
    # distinct all the way through so an unreachable repo can never present as
    # a clean shape. Neither value is on the allowlist, so both route to human
    # review — but only one of them is a fact about the PR.
    if paths is None:
        return "unreadable"

    paths = [p for p in paths if p]
    if not paths:
        return "empty"

    # Shape uses the same strict all-paths rule as every other shape, so a
    # 197-file hardening PR that happens to touch one post is `mixed`, not
    # `content`. The safety property does NOT live here — it lives in
    # content_veto(), which blocks auto-merge on ANY content path regardless
    # of shape. Keeping the two separate means the dashboard can label a PR
    # honestly without weakening the gate.
    if all(is_content(p) for p in paths):
        return "content"

    if all(is_lockfile(p) for p in paths):
        return "lockfile-only"

    if all(is_test(p) for p in paths):
        return "tests-only"

    if data_dirs:
        dd = [d.rstrip("/") for d in data_dirs]
        if all(any(p == d or p.startswith(d + "/") for d in dd) for p in paths):
            return "data-artifact-only"

    if all(is_ci(p) for p in paths):
        return "ci-fix-candidate"  # promote_candidate() judges the versions

    # Manifest + its lockfile. Deliberately BELOW lockfile-only, so a pure
    # lockfile diff keeps its existing (already-approved) shape and is not
    # newly subjected to the version rule. This branch is only reached when a
    # manifest is genuinely part of the diff — i.e. a declared dependency
    # changed, not just a transitive one.
    if all(is_manifest(p) or is_lockfile(p) for p in paths):
        return "dependency-bump-candidate"  # promote_candidate() judges it

    return "mixed"


def content_veto(paths):
    """True if ANY changed path is editorial content.

    Independent of shape, and absolute: content is a human judgement call, so
    it never auto-merges no matter how clean the rest of the diff looks. This
    is what stops a mostly-tests PR from sneaking a blog post past the gate.

    An unreadable diff (None) vetoes: "I could not check for content" must
    never resolve to "there is no content".
    """
    if paths is None:
        return True
    return any(is_content(p) for p in paths)


def auto_mergeable(shape, paths):
    return shape in ALLOWLIST and not content_veto(paths)


def ci_fix_allowed(diff_text):
    """A ci-fix-candidate only becomes ci-fix if its diff matches the whitelist."""
    added = [l for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")]
    if not added:
        return False
    return all(
        any(re.search(pat, line) for pat in CI_FIX_WHITELIST)
        for line in added
    )


def gh_json(*args):
    r = _run_gh(["gh", *args])
    if r.returncode != 0:
        return None
    return json.loads(r.stdout) if r.stdout.strip() else None


REPOS_YML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "repos.yml")


def parse_repos_yml(path=REPOS_YML):
    """repos.yml's `repos:` block as {key: {loops, paused, base, status, data_dir}}.

    A real YAML load, not a regex sweep. The regex version this replaces was
    correct on the file as written and silently wrong on the file as edited:
    re-indenting to four spaces, or writing one entry block-style instead of
    as a flow mapping, dropped entries with no error at all -- and a dropped
    entry for an archived repo is invisible to every consumer, because it
    appears in neither the drift list nor the coverage list. The audit exited
    0 reporting "no drift" on a parse that had found nothing.

    safe_load never writes, so the comments carrying this file's audit
    history (which base_branch was dropped, which PR it stranded) are
    untouched -- that was the stated reason for hand-parsing, and it was
    wrong.

    Raises SystemExit if PyYAML is absent: a caller asking what repos.yml
    says must never receive a guess. load_data_dirs() below deliberately
    softens that for consumers who only want an optional enrichment.
    """
    try:
        import yaml
    except ImportError:
        raise SystemExit("repos.yml needs PyYAML to parse: pip install pyyaml")
    with open(path) as f:
        repos = (yaml.safe_load(f) or {}).get("repos") or {}
    out = {}
    for key, body in repos.items():
        body = body or {}
        out[key] = {
            "loops": list(body.get("loops") or []),
            "paused": bool(body.get("paused", False)),
            "base": body.get("base_branch"),
            "status": body.get("status", "?"),
            "data_dir": list(body.get("data_dir") or []),
        }
    return out


def load_data_dirs():
    """Optional `data_dir` declarations, {repo: [dirs]}.

    Forgiving on purpose, unlike parse_repos_yml: this only enriches the
    `data-artifact-only` shape check, so a missing file or missing PyYAML
    should degrade to "no data dirs declared" rather than halt a merge sweep.
    """
    try:
        return {k: e["data_dir"] for k, e in parse_repos_yml().items()
                if e["data_dir"]}
    except (OSError, SystemExit):
        return {}


_TRANSIENT = ("connection reset", "read tcp", "timeout", "eof",
              "tls: failed to verify", "certificate signed by unknown",
              "no such host",
              "rate limit", "secondary rate", "was submitted too quickly",
              "502", "503", "504", "bad gateway", "temporarily unavailable")


def _run_gh(argv, retries=4):
    """One gh call, retrying transient network/rate failures.

    all_repos() deliberately RAISES rather than returning a partial fleet, so
    a single reset TCP connection would otherwise abort a whole sweep. The
    strictness is right; it just needs to not fire on a blip.
    """
    delay = 2.0
    for attempt in range(retries + 1):
        r = gh_transport.run(argv)
        if r.returncode == 0:
            return r
        if attempt < retries and any(s in (r.stderr or "").lower()
                                     for s in _TRANSIENT):
            time.sleep(delay)
            delay *= 2
            continue
        return r
    return r


def is_dependabot(pr):
    """Author-identity check that works across BOTH gh subcommands.

    `gh search prs` reports dependabot's login as `dependabot`; `gh pr list`
    reports the identical author as `app/dependabot`. A naive
    `login.startswith("dependabot")` therefore matches every bot PR under one
    command and none under the other — it silently returned 0 of 339 here.
    Match on the substring and let `is_bot` keep a human named e.g.
    "dependabot-fan" out of the bot bucket.
    """
    author = pr.get("author") or {}
    login = (author.get("login") or "").lower()
    if not login:
        return False
    if "dependabot" not in login:
        return False
    return author.get("is_bot", True)


def is_conflict(mergeable_state):
    """Only "dirty" is a conflict verdict. "unknown", "", and None mean
    GitHub has not answered yet, and the absence of an answer is never
    rendered as one. One definition, so the gate's verdict and every
    report reading the same field cannot drift apart.
    """
    return (mergeable_state or "").strip() == "dirty"


def conflict_self_heals(pr):
    """Which conflicted PRs resolve without a human: dependabot's, because
    the gate can ask it to rebase. Loop-produced auto/* PRs get no kick —
    only their loop can rewrite them. The gate reads this to act; triage
    reads it to bucket. Changing the policy is one edit both follow.
    """
    return is_dependabot(pr)


def all_repos():
    """Every non-archived repo under any OWNERS entry, from the repo-list API.

    Deliberately NOT a hand-maintained repo list: when this fleet kept one,
    11 of its 79 keys no longer resolved to a real repo, one recorded a name
    GitHub had since renamed away, and anything created after the last
    hand-edit was invisible to it. The repo list is generated by GitHub and
    cannot drift.

    Archived repos are excluded because every write against them 403s; there
    is no point enumerating PRs the gate could never act on.

    An owner that yields nothing raises rather than contributing zero repos. A
    typo, a renamed org and a dropped token scope all present as "no repos", and
    quietly enumerating one owner where two were configured is precisely how a
    whole org stays out of the queue without anything looking wrong.
    """
    repos = []
    for owner in require_owners():
        r = _run_gh(
            ["gh", "repo", "list", owner, "--limit", "500", "--no-archived",
             "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"])
        if r.returncode != 0:
            raise RuntimeError(
                f"cannot enumerate repos for {owner}: {r.stderr.strip()}")
        found = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if not found:
            raise RuntimeError(
                f"{owner} returned zero non-archived repos — refusing to run "
                "against a partial fleet")
        repos.extend(found)
    return sorted(repos)


def repo_visibility():
    """{nameWithOwner: "PUBLIC"|"PRIVATE"} for every non-archived repo.

    One call per owner. Callers need this to protect the capped ~2000 private
    Actions min/month — public repos have unlimited minutes, so anything
    CI-heavy should drain there first.

    A missing entry here is read by callers as "not public" and skips the repo
    under ONLY_PUBLIC, so an owner this failed to read costs coverage rather
    than safety. That is the intended direction, but it is why the map must
    cover every owner rather than just the first.
    """
    vis = {}
    for owner in OWNERS:
        r = _run_gh(
            ["gh", "repo", "list", owner, "--limit", "500", "--no-archived",
             "--json", "nameWithOwner,visibility"])
        if r.returncode != 0:
            continue
        vis.update({x["nameWithOwner"]: x["visibility"]
                    for x in json.loads(r.stdout or "[]")})
    return vis


def _search_total(query):
    """Ground-truth open-PR count from the Search API's own total_count."""
    r = _run_gh(
        ["gh", "api", f"search/issues?q={query}&per_page=1", "--jq", ".total_count"])
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def fetch_open_prs():
    """Open PRs the gate should consider — enumerated PER REPO, never searched.

    `gh search prs --limit N` truncates at N silently: no error, no warning,
    no partial-results flag. The previous implementation asked for 100 and got
    exactly 100 of the 407 open PRs, so the gate was reasoning about a quarter
    of the queue while reporting totals as if they were complete. Raising the
    limit does not fix it — the Search API hard-caps at 1000 results across
    ALL pages regardless of what you ask for, and 342 dependabot PRs plus
    human PRs is already close enough to that ceiling to be unsafe.

    `gh pr list --repo X` reads the repo's own PR index instead. It paginates
    honestly and has no fleet-wide cap, so the only truncation risk is a single
    repo holding more than PER_REPO_LIMIT open PRs — which the assertion below
    turns into a loud failure rather than a silent undercount.

    Dependabot's PRs are included by default; INCLUDE_BOTS=0 leaves them
    out. They were opt-in at first, and the opt-in was the bug: dependabot
    opens roughly four fifths of the actionable queue, so every run that
    forgot the flag reported a drained queue that was nothing of the kind.
    Nothing about the policy is relaxed for bots: they still need an
    allowlisted shape and a green required check, exactly like anything else.
    """
    PER_REPO_LIMIT = 300
    include_bots = os.environ.get("INCLUDE_BOTS", "1") != "0"
    # `body` is required: it is the only place dependabot states the version
    # deltas for a group bump, whose title names no versions at all.
    fields = "number,title,body,labels,createdAt,author,isDraft,headRefName"

    prs, truncated = [], []
    for repo in all_repos():
        rows = gh_json("pr", "list", "--repo", repo, "--state", "open",
                       "--limit", str(PER_REPO_LIMIT), "--json", fields) or []
        if len(rows) >= PER_REPO_LIMIT:
            truncated.append(repo)
        for pr in rows:
            if is_dependabot(pr) and not include_bots:
                continue
            # Re-attach the field the rest of the pipeline reads. `gh pr list`
            # omits it because the repo is implied by the query; `gh search`
            # supplied it, and every caller still expects that shape.
            pr["repository"] = {"nameWithOwner": repo, "name": repo.split("/")[1]}
            prs.append(pr)

    if truncated:
        raise RuntimeError(
            f"per-repo PR limit hit in {truncated} — raise PER_REPO_LIMIT; "
            "refusing to run against a truncated queue")

    # Cross-check against the Search API's total_count. total_count is exact
    # even when the result PAGES are capped, so it is a usable oracle for
    # "did we actually see everything?" without being usable to fetch them.
    # Repeated `user:` qualifiers OR together, and `user:` matches an org as
    # well as an account (verified: gr8monk3ys=322, Vivancedata=12, both=334).
    # The oracle must span the same owners as the enumeration, or it reports a
    # smaller expectation than reality and the "did we see everything?" check
    # passes by being asked the wrong question.
    scope = "+".join(f"user:{o}" for o in OWNERS)
    expected = _search_total(f"is:pr+is:open+{scope}+author:app/dependabot")
    if include_bots and expected is not None:
        got = sum(1 for p in prs if is_dependabot(p))
        # Archived repos are excluded above but still counted by search, so
        # seeing FEWER is expected; seeing more means the enumeration is wrong.
        if got > expected:
            raise RuntimeError(f"enumerated {got} dependabot PRs but search "
                               f"reports {expected} — enumeration is wrong")
        if expected - got > 0:
            print(f"note: {expected - got} dependabot PR(s) sit in archived "
                  f"repos and are excluded (search total={expected}, "
                  f"actionable={got})", file=sys.stderr)
    return prs


def changed_paths(repo, number):
    """Changed file paths, or None if the API could not be read.

    None and [] mean different things and must never be conflated. This used
    to `return []` on any failure, so a 404 (seo-wordbubble's remote has been
    gone since 2026-08-03) or a transient rate-limit came back as a clean
    `empty` shape — the gate recording "there is nothing in this diff" when
    the truth was "I could not look". It fails safe today only because
    `empty` is not on the allowlist; that is luck, not design.
    """
    # NOT --jq '[.[].filename]': with --paginate that emits one JSON array PER
    # PAGE, and the concatenation is not valid JSON. Emit one filename per line
    # and split, which paginates cleanly for PRs of any size.
    r = _run_gh(
        ["gh", "api", f"repos/{repo}/pulls/{number}/files",
         "--paginate", "--jq", ".[].filename"])
    if r.returncode != 0:
        return None
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


# Live fixtures: (owner/repo, PR number, expected promoted shape). These run
# against the real GitHub API under --validate, so they belong to whoever
# operates the gate -- add PRs from your own fleet whose shape you have
# verified by hand. The pure-function cases in selftest() always run.
#
# A fixture whose repo has since been deleted or renamed SKIPS rather than
# FAILS: that is not a classifier defect and must not read as one. (One used
# to cover `tests-only` until its remote 404'd, and reported FAIL for months
# for a reason that had nothing to do with the classifier.)
VALIDATION_CASES = [
    # ("owner/repo", 123, "mixed"),
]


def selftest():
    """Pure-function checks — no network, so the gate logic stays verifiable."""
    cases = [
        (["bun.lockb"], None, "lockfile-only", True),
        (["src/a.test.ts", "src/b.test.ts"], None, "tests-only", True),
        (["src/a.test.ts", "src/main.ts"], None, "mixed", False),
        (["content/posts/x.md"], None, "content", False),
        # veto: clean tests-only shape, but a post rode along -> not mergeable
        (["tests/x_test.py", "content/posts/x.md"], None, "mixed", False),
        (["data/out.csv"], ["data"], "data-artifact-only", True),
        (["data/out.csv"], None, "mixed", False),   # unset data_dir => deny
        ([], None, "empty", False),
        # "could not read the diff" is its own shape and vetoes. Distinct from
        # `empty`, which is a real (if odd) fact about a real PR.
        (None, None, "unreadable", False),
        # requirements.txt is a MANIFEST. As a "lockfile" it let major Python
        # bumps (pytest 7->9, cryptography 48->50) auto-merge unversioned.
        (["requirements.txt"], None, "dependency-bump-candidate", False),
        (["requirements-dev.txt"], None, "dependency-bump-candidate", False),
        (["uv.lock"], None, "lockfile-only", True),
        (["pyproject.toml", "uv.lock"], None, "dependency-bump-candidate", False),
    ]
    bad = 0
    for paths, dd, want_shape, want_merge in cases:
        shape = classify(paths, dd)
        merge = auto_mergeable(shape, paths)
        if shape != want_shape or merge != want_merge:
            bad += 1
            print(f"FAIL {paths} dd={dd}: shape={shape}(want {want_shape}) "
                  f"mergeable={merge}(want {want_merge})")
    print(f"selftest(shape): {len(cases)-bad}/{len(cases)} passed")
    return bad + _selftest_bumps()


# Bodies below are trimmed verbatim from real dependabot PRs, so the parser is
# tested against the formats GitHub actually emits rather than idealised ones.
_BODY_SINGLE = "Bumps [fast-uri](https://github.com/x/y) from 3.1.2 to 3.1.5.\n"
_BODY_SHA = ("Bumps [github/codeql-action/init](https://github.com/github/codeql-action) "
             "from c3400c2f38909e0dcf3c3a41f2030a8217be5d3e to "
             "c4dd10e44af883a891fe31ced449bcb4a6728b9b.\n")
_BODY_GROUP_SAFE = """Bumps the development-dependencies group with 2 updates:

| Package | From | To |
| --- | --- | --- |
| [@vitest/ui](https://github.com/vitest-dev/vitest) | `4.1.8` | `4.1.10` |
| [eslint](https://github.com/eslint/eslint) | `9.39.4` | `9.41.0` |
"""
# ^ one patch and one minor: the group must score `minor`, proving the result
#   is the maximum across packages rather than the first row parsed.
# Same shape, but two majors hide inside — the whole group must be blocked.
_BODY_GROUP_MAJOR = """Bumps the development-dependencies group with 3 updates:

| Package | From | To |
| --- | --- | --- |
| [@vitest/ui](https://github.com/vitest-dev/vitest) | `4.1.8` | `4.1.10` |
| [@testing-library/jest-dom](https://github.com/x/y) | `6.9.1` | `7.0.0` |
| [jsdom](https://github.com/jsdom/jsdom) | `28.1.0` | `30.0.1` |
"""
_BODY_ANCESTOR = ("Bumps [postcss](https://github.com/postcss/postcss) to 8.5.23 and "
                  "updates ancestor dependency [next](https://github.com/vercel/next.js).\n\n"
                  "Updates `postcss` from 8.4.31 to 8.5.23\n"
                  "Updates `next` from 14.0.0 to 16.2.11\n")


def _selftest_bumps():
    """Version-policy checks. Pure functions, no network."""
    cases = [
        # (title, body, expected bump_kind)
        ("bump fast-uri from 3.1.2 to 3.1.5 in /mobile", _BODY_SINGLE, "patch"),
        ("bump actions/setup-node from 6.2.0 to 6.4.0", "", "minor"),
        ("bump actions/checkout from 4.3.1 to 7.0.1", "", "major"),
        ("Bump actions/setup-python from 5 to 7", "", "major"),
        # 0.x: minor position is breaking under semver's own rules.
        ("bump uvicorn from 0.27.0 to 0.41.0", "", "major"),
        ("bump esbuild from 0.27.7 to 0.27.9", "", "patch"),
        # 0.x -> 1.x is a major by the major position alone.
        ("bump starlette from 0.52.1 to 1.3.1", "", "major"),
        # SHA-pinned actions carry no version to compare.
        ("bump github/codeql-action/init from c3400c2f to c4dd10e4", _BODY_SHA, "sha"),
        # Group bumps take the WORST delta inside them, not the first.
        ("bump the development-dependencies group with 2 updates", _BODY_GROUP_SAFE, "minor"),
        ("bump the development-dependencies group with 3 updates", _BODY_GROUP_MAJOR, "major"),
        ("bump postcss and next in /site", _BODY_ANCESTOR, "major"),
        # Range updates ("to >=9") have no comparable target version.
        ("Update pytest requirement from <8.0.0,>=7.4.4 to >=9", "", "unknown"),
        # Not a dependency bump at all.
        ("fix: correct the retry backoff", "", None),
    ]
    bad = 0
    for title, body, want in cases:
        got = bump_kind(title, body)
        if got != want:
            bad += 1
            print(f"FAIL bump_kind({title[:48]!r}) = {got!r}, want {want!r}")

    # Promotion: candidate shapes resolve by version, majors fall to `mixed`.
    promo = [
        ("dependency-bump-candidate", "bump x from 1.2.3 to 1.2.4", "", "dependency-bump"),
        ("dependency-bump-candidate", "bump x from 1.2.3 to 2.0.0", "", "mixed"),
        ("ci-fix-candidate", "bump actions/setup-node from 6.2.0 to 6.4.0", "", "dependency-bump"),
        ("ci-fix-candidate", "bump actions/checkout from 4.3.1 to 7.0.1", "", "mixed"),
        # No bump metadata and no diff to check the whitelist against -> deny.
        ("ci-fix-candidate", "ci: minute-safe Actions policy", "", "mixed"),
        # No bump metadata (loop-produced lockfile refresh) -> unchanged.
        ("lockfile-only", "chore: refresh lockfile", "", "lockfile-only"),
        # A pure uv.lock diff that crosses two majors is still a major.
        ("lockfile-only", "bump cryptography from 48.0.1 to 50.0.0", "", "mixed"),
        ("lockfile-only", "bump undici from 7.28.0 to 7.29.0", "", "lockfile-only"),
    ]
    for shape, title, body, want in promo:
        got = promote_candidate(shape, title, body)
        if got != want:
            bad += 1
            print(f"FAIL promote({shape}, {title[:40]!r}) = {got!r}, want {want!r}")

    # A promoted dependency-bump must actually be allowlisted, and a group
    # containing a major must not be.
    checks = [
        (promote_candidate("dependency-bump-candidate", "", _BODY_GROUP_SAFE),
         ["package.json", "package-lock.json"], True),
        (promote_candidate("dependency-bump-candidate", "", _BODY_GROUP_MAJOR),
         ["package.json", "package-lock.json"], False),
    ]
    for shape, paths, want in checks:
        if auto_mergeable(shape, paths) != want:
            bad += 1
            print(f"FAIL auto_mergeable({shape}, {paths}) != {want}")

    total = len(cases) + len(promo) + len(checks)
    print(f"selftest(bump): {total-bad}/{total} passed")
    return bad


def main():
    as_json = "--json" in sys.argv
    validate = "--validate" in sys.argv
    data_dirs = load_data_dirs()

    if validate:
        failures = selftest()
        print("\n=== classifier validation against known cases ===")
        if not VALIDATION_CASES:
            print("(no live fixtures configured -- add PRs from your own fleet "
                  "to VALIDATION_CASES to check the classifier against them)")
        skipped = 0
        for repo, num, expected in VALIDATION_CASES:
            paths = changed_paths(repo, num)
            if paths is None:
                # The fixture is gone (repo deleted/renamed) or the API is
                # unreachable. That is not a classifier defect, so it must not
                # read as one — but it is also not a pass, so it is counted.
                skipped += 1
                print(f"SKIP  {repo}#{num}: fixture unreachable (repo gone or API error)")
                continue
            # Validate the shape the GATE acts on, which is the promoted one.
            # Checking classify() alone would pass while the PR still merged
            # (or didn't) for reasons this suite never looked at.
            meta = gh_json("pr", "view", str(num), "--repo", repo,
                           "--json", "title,body") or {}
            got = promote_candidate(
                classify(paths, data_dirs.get(repo.split("/")[1])),
                meta.get("title", ""), meta.get("body", "") or "")
            ok = "PASS" if got == expected else "FAIL"
            if got != expected:
                failures += 1
            print(f"{ok}  {repo}#{num}: expected={expected} got={got}")
            print(f"      paths: {paths[:6]}{' …' if len(paths) > 6 else ''}")
        checked = len(VALIDATION_CASES) - skipped
        print(f"\n{checked - failures}/{checked} checked passed"
              + (f", {skipped} skipped" if skipped else ""))
        return 1 if failures else 0

    rows = []
    for pr in fetch_open_prs():
        repo = pr["repository"]["nameWithOwner"]
        name = repo.split("/")[1]
        paths = changed_paths(repo, pr["number"])
        shape = promote_candidate(classify(paths, data_dirs.get(name)),
                                  pr.get("title", ""), pr.get("body", ""))
        rows.append({
            "repo": repo, "number": pr["number"], "title": pr["title"],
            "shape": shape, "files": -1 if paths is None else len(paths),
            "bump": bump_kind(pr.get("title", ""), pr.get("body", "")),
            "labels": [l["name"] for l in pr.get("labels", [])],
            "createdAt": pr["createdAt"],
            "auto_mergeable": auto_mergeable(shape, paths),
            "content_veto": content_veto(paths),
        })

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    by_shape = {}
    for r in rows:
        by_shape.setdefault(r["shape"], []).append(r)
    for shape in sorted(by_shape):
        print(f"\n=== {shape}  ({len(by_shape[shape])}) ===")
        for r in sorted(by_shape[shape], key=lambda x: x["createdAt"]):
            print(f"  {r['repo']}#{r['number']:<4} {r['files']:>3}f  {r['title'][:64]}")
    print(f"\ntotal: {len(rows)} open PRs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
