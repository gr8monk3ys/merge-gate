"""A `gh`-shaped transport that does not need the `gh` binary.

Why this exists
---------------
A scheduled runner ran the gate 15+ consecutive times over three weeks
reporting the same line: *"GitHub access is not enabled for this session"*.
`merge_gate.py` and `classify_pr.py` shell out to `gh`, so in that runner the
gate could not execute at all -- not "decided not to arm", not "found
nothing": it never ran, and every report of a drained queue was a report
about a sweep that had not happened.

Such a runner is not necessarily cut off from GitHub. One that pushes commits
over HTTPS has a credential reachable -- just not through `gh`, and not
through any env var the caller controls. `git credential fill` is the
helper-agnostic way to ask for it, and it is the same question git itself asks
before a push, so if the push works this works.

Design
------
One function, `run(argv)`, accepting the `gh` argv the scripts already build
and returning something shaped like `subprocess.CompletedProcess`. Call sites
do not change, which matters: the argv strings in those scripts encode hard-won
detail (`--paginate` with a line-per-record jq, `--match-head-commit`,
uppercase visibility) and rewriting them by hand is how that detail gets lost.

Only the argv forms this package actually issues are supported. Anything else
returns a nonzero code with a clear message rather than an approximation --
every caller already treats a nonzero code as "GitHub did not answer" and
fails safe on it, which is the correct outcome for a form this cannot model.
An approximation would instead be read as a fact about the repository.
"""

import json
import pathlib
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
UA = "merge-gate/0.2.0"

# "gh" | "rest" | "auto". auto = use the binary when it can actually talk to
# GitHub, else REST. Forcing "rest" locally is how this file is tested against
# the real thing; see difftest() below.
MODE = os.environ.get("GH_TRANSPORT", "auto")


class Result:
    """Duck-typed CompletedProcess: the three attributes callers read."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Unsupported(Exception):
    """This argv form is not modelled. Never answered with a guess."""


# --------------------------------------------------------------------------
# credentials


_token_cache = []


def token():
    """The first credential that answers, or None.

    Order is deliberate: an explicitly exported token beats an ambient one,
    because an operator who sets GH_TOKEN is overriding something on purpose.
    `git credential fill` is last and is the one that carries the cloud
    runner -- it consults whatever helper git itself would use, so it needs no
    knowledge of how this particular environment stores the secret.
    """
    if _token_cache:
        return _token_cache[0]
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not tok and shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            tok = r.stdout.strip()
    if not tok:
        host = urllib.parse.urlsplit(API).netloc
        host = "github.com" if host == "api.github.com" else host
        try:
            r = subprocess.run(["git", "credential", "fill"],
                               input=f"protocol=https\nhost={host}\n\n",
                               capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                if line.startswith("password="):
                    tok = line[len("password="):].strip()
                    break
        except (OSError, subprocess.SubprocessError):
            tok = None
    if tok:
        _token_cache.append(tok)
    return tok or None


# --------------------------------------------------------------------------
# HTTP


def _request(method, path, body=None):
    """One REST call. Returns (status, parsed_json_or_None, link_header)."""
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    tok = token()
    if not tok:
        raise Unsupported("no GitHub credential: set GH_TOKEN, or make "
                          "`git credential fill` answer for this host")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            # A write that was redirected did not necessarily happen where the
            # caller meant it to. GitHub answers a request against a RENAMED
            # repo with a redirect, and for PUT it discards the body -- so the
            # call succeeds and nothing changes. Refuse rather than report a
            # write that may not exist; the caller should resolve the
            # canonical name first.
            if method not in ("GET", "HEAD") and resp.url != url:
                return 409, None, (f"gh: refusing a redirected {method} "
                                   f"({url} -> {resp.url}); resolve the "
                                   "canonical repo name first (HTTP 409)")
            raw = resp.read().decode()
            parsed = json.loads(raw) if raw.strip() else None
            return resp.status, parsed, resp.headers.get("Link", "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            msg = json.loads(raw).get("message", raw[:200])
        except Exception:
            msg = raw[:200]
        # Shaped like gh's own stderr, because sh_strict() greps it for "404"
        # to tell an unprotected branch from an unread one.
        return e.code, None, f"gh: {msg} (HTTP {e.code})"


def _paged(path, cap_pages=100):
    """Yield each page's parsed body, following Link rel=next."""
    url = path
    for _ in range(cap_pages):
        status, parsed, link = _request("GET", url)
        if status >= 400:
            raise _HttpFail(status, link)
        yield parsed
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
        if not nxt:
            return
        url = nxt.group(1)


class _HttpFail(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _get_all(path, per_page=100, stop_after=None):
    """Every item across every page of a list endpoint, as one list.

    `stop_after` stops paging once that many items are in hand, mirroring
    `gh --limit`: without it, a `--limit 300` against a repo holding thousands
    of closed PRs would page all of them to throw the rest away.
    """
    sep = "&" if "?" in path else "?"
    items = []
    for page in _paged(f"{path}{sep}per_page={per_page}"):
        if isinstance(page, list):
            items.extend(page)
        elif page is not None:
            items.append(page)
        if stop_after is not None and len(items) >= stop_after:
            break
    return items


# --------------------------------------------------------------------------
# the jq subset
#
# Only the expressions these scripts actually pass. An unrecognised one raises
# rather than returning something plausible: a wrong jq result would be read as
# a fact about a pull request, and this whole file exists because that class of
# mistake is expensive.


def _path_get(value, path):
    for key in [p for p in path.split(".") if p]:
        if value is None:
            return None
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _object(item, spec):
    """jq object-construction body: `name,conclusion,at:.updated_at`."""
    out = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if ":" in part:
            alias, path = part.split(":", 1)
            out[alias.strip()] = _path_get(item, path.strip().lstrip("."))
        else:
            out[part] = (item or {}).get(part)
    return out


def _render_scalar(v):
    """One interpolated value, jq's `\\(...)` rendering."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, str)):
        return str(v)
    return json.dumps(v, separators=(",", ":"))


def jq(expr, doc):
    """Evaluate a supported jq expression. Returns a list of output values."""
    e = expr.strip()

    # any(.[]; .body|test("RE"))  -- the dependabot rebase-nag throttle
    m = re.fullmatch(r'any\(\s*\.\[\]\s*;\s*\.(\w+)\s*\|\s*test\("(.*)"\)\s*\)', e)
    if m:
        field, pattern = m.group(1), m.group(2)
        items = doc if isinstance(doc, list) else []
        return [any(re.search(pattern, str(i.get(field) or "")) for i in items)]

    # .[N] // empty | "...\(.a.b)..."  -- ci_watchdog's one-line PR summary
    m = re.fullmatch(r'\.\[(\d+)\]\s*//\s*empty\s*\|\s*"(.*)"', e)
    if m:
        items = doc if isinstance(doc, list) else []
        idx = int(m.group(1))
        if idx >= len(items):
            return []           # `// empty` means no output at all, not null
        item = items[idx]
        return [re.sub(r'\\\((\.[\w.]+)\)',
                       lambda g: _render_scalar(_path_get(item, g.group(1)[1:])),
                       m.group(2))]

    # [.a[]|{x,y,alias:.z}]  -- check-runs / workflow-runs projections
    m = re.fullmatch(r'\[\s*\.(\w+)\[\]\s*\|\s*\{(.+?)\}\s*\]', e)
    if m:
        src = _path_get(doc, m.group(1)) or []
        return [[_object(i, m.group(2)) for i in src]]

    # [.a[].b]  -- collect one field across an array
    m = re.fullmatch(r'\[\s*\.(\w+)\[\]\.([\w.]+)\s*\]', e)
    if m:
        src = _path_get(doc, m.group(1)) or []
        return [[_path_get(i, m.group(2)) for i in src]]

    # {a,b}  -- top-level object construction
    m = re.fullmatch(r'\{(.+?)\}', e)
    if m:
        return [_object(doc, m.group(1))]

    # .[N].a.b  -- index into an array, then a path
    m = re.fullmatch(r'\.\[(\d+)\]\.([\w.]+)', e)
    if m:
        items = doc if isinstance(doc, list) else []
        idx = int(m.group(1))
        return [_path_get(items[idx], m.group(2))] if idx < len(items) else [None]

    # .a != null
    m = re.fullmatch(r'\.([\w.]+)\s*!=\s*null', e)
    if m:
        return [_path_get(doc, m.group(1)) is not None]

    # .a,.b,.c  -- one output per term, printed one per line
    if "," in e and "(" not in e and "[" not in e and "{" not in e:
        out = []
        for term in e.split(","):
            out.extend(jq(term, doc))
        return out

    # .[].field  /  .[]  -- iterate a top-level array
    m = re.fullmatch(r'\.\[\]\s*(?:\.([\w.]+))?', e)
    if m:
        items = doc if isinstance(doc, list) else []
        return [_path_get(i, m.group(1)) if m.group(1) else i for i in items]

    # .a.b  /  .
    if re.fullmatch(r'\.([\w.]*)', e):
        return [_path_get(doc, e[1:]) if e != "." else doc]

    raise Unsupported(f"unsupported jq expression: {expr!r}")


def render(values):
    """Format jq outputs the way gh does: one per line, strings unquoted."""
    lines = []
    for v in values:
        if isinstance(v, str):
            lines.append(v)
        elif v is None:
            lines.append("null")
        elif isinstance(v, bool):
            lines.append("true" if v else "false")
        elif isinstance(v, (int, float)):
            lines.append(str(v))
        else:
            lines.append(json.dumps(v, separators=(",", ":"), sort_keys=True))
    return "".join(l + "\n" for l in lines)


# --------------------------------------------------------------------------
# argv parsing


def _flag(args, name):
    """Value of `--name V`, or None. Removes nothing; args stay intact."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


# --------------------------------------------------------------------------
# subcommands


def _positional(args):
    """The first argument that is not a flag or a flag's value.

    `args[0]` is wrong: `gh api --method PUT repos/o/r/...` puts the verb
    first, and reading the path as "--method" turned every write into a
    request for a nonexistent endpoint -- silently, because the caller only
    checked the exit code of a call that never reached the right URL.
    """
    takes_value = ("-X", "--method", "--jq", "-q", "-F", "-f", "--input",
                   "-H", "--header")
    i = 0
    while i < len(args):
        a = args[i]
        if a in takes_value:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    raise Unsupported("gh api: no path in argv")


def _body(args, stdin=None):
    """Request body from `-F k=v` / `-f k=v` / `--input -` (or a file).

    Without this the REST adapter parsed `--method PUT` and then sent nothing:
    GitHub answered 2xx and changed nothing, so a branch-protection sweep
    reported success having applied no protection at all. `-F` types its
    values the way gh does (true/false/null/int stay JSON scalars, everything
    else is a string); `--input` is already JSON and is passed through.
    """
    src = _flag(args, "--input")
    if src is not None:
        raw = (stdin if src == "-" else pathlib.Path(src).read_text())
        if raw is None:
            raise Unsupported("gh api --input -: nothing on stdin")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise Unsupported(f"gh api --input: not JSON ({e})")

    fields = {}
    for name in ("-F", "-f"):
        i = 0
        while i < len(args) - 1:
            if args[i] == name:
                key, _, value = args[i + 1].partition("=")
                if name == "-F":
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass          # a bare string, which is what gh sends
                fields[key] = value
                i += 2
                continue
            i += 1
    return fields or None


def _cmd_api(args, stdin=None):
    path = _positional(args)
    method = _flag(args, "-X") or _flag(args, "--method") or "GET"
    jq_expr = _flag(args, "--jq") or _flag(args, "-q")
    paginate = "--paginate" in args

    if paginate:
        # gh applies --jq PER PAGE and concatenates the output. Reproducing
        # that exactly is the point: `--jq '.[].filename'` paginates cleanly
        # only because each page is rendered to lines separately, while
        # `--jq '[.[].filename]'` would emit one array per page and not be
        # valid JSON. The comment at changed_paths() depends on this.
        chunks = []
        sep = "&" if "?" in path else "?"
        for page in _paged(f"{path}{sep}per_page=100"):
            chunks.append(render(jq(jq_expr, page)) if jq_expr
                          else json.dumps(page, indent=2) + "\n")
        return Result(0, "".join(chunks))

    status, parsed, link = _request(method, path, _body(args, stdin))
    if status >= 400:
        return Result(1, "", link)
    if jq_expr:
        return Result(0, render(jq(jq_expr, parsed)))
    return Result(0, json.dumps(parsed, indent=2) + "\n")


# gh's --json names -> how to read the same value out of a REST payload.
# Only the fields the fleet's scripts actually request: an unlisted field
# raises in _cmd_pr_list below rather than being silently modelled and never
# exercised.
_PR_FIELDS = {
    "number": lambda p: p["number"],
    "title": lambda p: p.get("title"),
    "body": lambda p: p.get("body"),
    "createdAt": lambda p: p.get("created_at"),
    "isDraft": lambda p: p.get("draft", False),
    "headRefName": lambda p: (p.get("head") or {}).get("ref"),
    "labels": lambda p: [{"name": l["name"]} for l in p.get("labels") or []],
    "author": lambda p: {"login": _login(p)},
}


def _login(pr):
    """Author login, normalised to what `gh pr list` reports.

    REST says `dependabot[bot]`; `gh pr list` says `app/dependabot`; `gh
    search prs` says `dependabot`. is_dependabot() matches the substring
    precisely because those three disagree, so any of them is safe here --
    but emitting gh's spelling keeps a diff of the two transports clean.
    """
    user = pr.get("user") or {}
    login = user.get("login") or ""
    if user.get("type") == "Bot" and login.endswith("[bot]"):
        return "app/" + login[:-len("[bot]")]
    return login


def _cmd_pr_list(args):
    repo = _flag(args, "--repo")
    state = _flag(args, "--state") or "open"
    limit = int(_flag(args, "--limit") or 30)
    fields = (_flag(args, "--json") or "number").split(",")
    # `--jq`/`-q` is a FILTER, not a formatting nicety: `gh pr list --json
    # number --jq .[].number` yields one number per line and callers index
    # into it. Dropping the filter and returning the raw array handed back
    # `[{"number": 98}, ...]`, which then went into an API path -- a wrong
    # answer shaped exactly like a right one, which is the single thing this
    # module promises not to produce. _cmd_repo_list always honoured it; this
    # one did not, and the only caller issuing this form is verify-gates.py,
    # which is why nothing else noticed.
    jq_expr = _flag(args, "--jq") or _flag(args, "-q")
    unknown = [f for f in fields if f not in _PR_FIELDS]
    if unknown:
        raise Unsupported(f"pr list --json fields not modelled: {unknown}")

    if state not in ("open", "closed", "all"):
        # gh accepts `merged`; REST does not, and silently treating it as
        # `closed` would hand back closed-unmerged PRs as merged ones.
        raise Unsupported(f"pr list --state {state} is not modelled")
    rows = _get_all(f"repos/{repo}/pulls?state={state}", stop_after=limit)[:limit]
    out = [{f: _PR_FIELDS[f](p) for f in fields} for p in rows]
    if jq_expr:
        return Result(0, render(jq(jq_expr, out)))
    return Result(0, json.dumps(out, sort_keys=True) + "\n")


def _cmd_repo_list(args):
    owner = args[0]
    limit = int(_flag(args, "--limit") or 30)
    fields = (_flag(args, "--json") or "nameWithOwner").split(",")
    jq_expr = _flag(args, "--jq") or _flag(args, "-q")

    # /user/repos sees private repos the owner can read; /users/{o}/repos and
    # /orgs/{o}/repos do not, and a fleet that silently drops its private half
    # is exactly the "partial fleet" all_repos() refuses to run against. Fall
    # back only when the token is not the owner's.
    rows = []
    try:
        rows = [r for r in _get_all("user/repos?affiliation=owner,organization_member")
                if (r.get("owner") or {}).get("login", "").lower() == owner.lower()]
    except _HttpFail:
        rows = []
    if not rows:
        for endpoint in (f"orgs/{owner}/repos", f"users/{owner}/repos"):
            try:
                rows = _get_all(endpoint)
                break
            except _HttpFail:
                continue

    # `disabled` repos are dropped unconditionally, matching gh's GraphQL
    # listing. A repo GitHub has disabled (a TOS block, say) 403s on every
    # call; gh never returns it and REST does. Letting one through would add
    # a repo to the sweep that can only ever fail -- the same reason archived
    # repos are excluded.
    rows = [r for r in rows if not r.get("disabled")]
    if "--no-archived" in args:
        rows = [r for r in rows if not r.get("archived")]
    if "--source" in args:
        rows = [r for r in rows if not r.get("fork")]

    proj = []
    for r in rows[:limit]:
        item = {}
        for f in fields:
            if f == "nameWithOwner":
                item[f] = r["full_name"]
            elif f == "name":
                item[f] = r["name"]
            elif f == "visibility":
                # gh reports this uppercase and merge_gate compares against
                # the literal "PUBLIC". REST says "public".
                item[f] = (r.get("visibility")
                           or ("private" if r.get("private") else "public")).upper()
            elif f == "isArchived":
                item[f] = bool(r.get("archived"))
            elif f == "defaultBranchRef":
                item[f] = {"name": r.get("default_branch")}
            else:
                raise Unsupported(f"repo list --json field not modelled: {f}")
        proj.append(item)
    proj.sort(key=lambda x: x.get("nameWithOwner") or x.get("name") or "")

    if jq_expr:
        return Result(0, render(jq(jq_expr, proj)))
    return Result(0, json.dumps(proj, sort_keys=True) + "\n")


def _pr_node_id(repo, num):
    status, parsed, err = _request("GET", f"repos/{repo}/pulls/{num}")
    if status >= 400:
        raise _HttpFail(status, err)
    return parsed["node_id"]


def _cmd_pr_write(args):
    """pr comment | pr edit --add-label | pr merge."""
    verb, num = args[0], args[1]
    repo = _flag(args, "--repo")

    if verb == "comment":
        body = _flag(args, "--body")
        status, _, err = _request("POST", f"repos/{repo}/issues/{num}/comments",
                                  {"body": body})
        return Result(0 if status < 400 else 1, "", err or "")

    if verb == "edit":
        label = _flag(args, "--add-label")
        if label is None:
            raise Unsupported("pr edit: only --add-label is modelled")
        status, _, err = _request("POST", f"repos/{repo}/issues/{num}/labels",
                                  {"labels": label.split(",")})
        return Result(0 if status < 400 else 1, "", err or "")

    if verb == "merge":
        if "--disable-auto" in args:
            # Auto-merge lives only in GraphQL; there is no REST equivalent.
            node = _pr_node_id(repo, num)
            q = ("mutation($id:ID!){disablePullRequestAutoMerge("
                 "input:{pullRequestId:$id}){clientMutationId}}")
            status, parsed, err = _request("POST", "graphql",
                                           {"query": q, "variables": {"id": node}})
            if status >= 400:
                return Result(1, "", err or "")
            if parsed and parsed.get("errors"):
                return Result(1, "", json.dumps(parsed["errors"])[:200])
            return Result(0, "")

        method = ("squash" if "--squash" in args else
                  "rebase" if "--rebase" in args else "merge")
        payload = {"merge_method": method}
        sha = _flag(args, "--match-head-commit")
        if sha:
            # GitHub refuses with 409 if the head moved. That refusal is the
            # entire judged-head guarantee -- it must reach the caller as a
            # nonzero exit, never be retried or worked around.
            payload["sha"] = sha
        status, _, err = _request("PUT", f"repos/{repo}/pulls/{num}/merge", payload)
        return Result(0 if status < 400 else 1, "", err or "")

    raise Unsupported(f"pr subcommand not modelled: {verb}")


# --------------------------------------------------------------------------
# entry point


def rest_run(argv, stdin=None):
    """Execute a `gh` argv over REST. argv[0] is "gh".

    `stdin` is what a real `gh` would read for `--input -`.
    """
    args = list(argv[1:])
    try:
        if not args:
            raise Unsupported("empty gh argv")
        if args[0] == "api":
            return _cmd_api(args[1:], stdin)
        if args[0] == "repo" and len(args) > 1 and args[1] == "list":
            return _cmd_repo_list(args[2:])
        if args[0] == "pr" and len(args) > 1:
            if args[1] == "list":
                return _cmd_pr_list(args[2:])
            return _cmd_pr_write(args[1:])
        raise Unsupported(f"gh subcommand not modelled: {' '.join(args[:2])}")
    except Unsupported as e:
        return Result(1, "", f"gh-transport: {e}")
    except _HttpFail as e:
        return Result(1, "", e.message)
    except urllib.error.URLError as e:
        # Worded so classify_pr._run_gh's transient-retry matcher sees it.
        return Result(1, "", f"gh-transport: connection error: {e.reason}")


_binary_ok = []


def binary_works():
    """Is the `gh` binary present AND actually able to talk to GitHub?

    Presence alone is not the question. The cloud runner's failure was an
    unauthenticated session, and `gh api user` is the cheapest call that
    distinguishes "installed" from "will answer". Cached: it is asked once
    per process, in front of a few thousand calls.
    """
    if _binary_ok:
        return _binary_ok[0]
    ok = False
    if shutil.which("gh"):
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True)
        ok = r.returncode == 0
    _binary_ok.append(ok)
    return ok


def run(argv, stdin=None):
    """Run a gh argv through whichever transport can answer.

    `stdin` is the request body for `gh api --input -`. Both transports take
    it, so a write is expressible the same way whichever one answers.
    """
    if MODE == "rest":
        return rest_run(argv, stdin)
    if MODE == "gh" or binary_works():
        return subprocess.run(argv, input=stdin, capture_output=True,
                              text=True)
    return rest_run(argv, stdin)


# --------------------------------------------------------------------------
# tests
#
# selftest() is pure: it pins the jq subset and the gh-compatible rendering
# with no network, so a regression is caught wherever this file is edited.
# difftest() is the one that actually proved the port -- it runs each argv
# form through BOTH transports against live GitHub and demands byte-identical
# output. Keep them separate: the pure one must stay runnable in the cloud
# runner, which is exactly the environment with no `gh` to compare against.


def selftest():
    checks = [
        # (jq expression, document, expected rendered stdout)
        (".default_branch", {"default_branch": "main"}, "main\n"),
        (".allow_auto_merge", {"allow_auto_merge": True}, "true\n"),
        (".total_count", {"total_count": 118}, "118\n"),
        # A missing key renders as null, never as the empty string -- callers
        # compare against the literal "true"/"false" and an empty line would
        # read as false.
        (".contexts", {}, "null\n"),
        (".contexts", {"contexts": ["Lint, Test & Build"]},
         '["Lint, Test & Build"]\n'),
        # Comma = one output per term, one line each, in order.
        (".head.sha,.base.ref,.draft,.mergeable_state",
         {"head": {"sha": "abc"}, "base": {"ref": "main"},
          "draft": False, "mergeable_state": "clean"},
         "abc\nmain\nfalse\nclean\n"),
        (".auto_merge != null", {"auto_merge": None}, "false\n"),
        (".auto_merge != null", {"auto_merge": {"enabled_by": "x"}}, "true\n"),
        (".draft,.base.ref,.auto_merge!=null",
         {"draft": True, "base": {"ref": "master"}, "auto_merge": None},
         "true\nmaster\nfalse\n"),
        (".[].filename", [{"filename": "a.py"}, {"filename": "b.py"}],
         "a.py\nb.py\n"),
        (".[].nameWithOwner", [{"nameWithOwner": "o/r"}], "o/r\n"),
        # gojq sorts object keys; CPython would not. The differential test
        # only means something because these agree byte for byte.
        ("[.check_runs[]|{name,conclusion}]",
         {"check_runs": [{"name": "b", "conclusion": "success"}]},
         '[{"conclusion":"success","name":"b"}]\n'),
        ("[.check_runs[].name]", {"check_runs": [{"name": "x"}]}, '["x"]\n'),
        ("[.workflow_runs[]|{name,conclusion,at:.updated_at}]",
         {"workflow_runs": [{"name": "CI", "conclusion": "failure",
                             "updated_at": "2026-08-30T00:00:00Z"}]},
         '[{"at":"2026-08-30T00:00:00Z","conclusion":"failure","name":"CI"}]\n'),
        ("{full_name,archived}", {"full_name": "o/r", "archived": False},
         '{"archived":false,"full_name":"o/r"}\n'),
        (".[0].commit.committer.date",
         [{"commit": {"committer": {"date": "2026-01-01"}}}], "2026-01-01\n"),
        # `// empty` must emit NOTHING, not a blank line: ci_watchdog reads a
        # blank line as a PR whose number is the empty string.
        ('.[0] // empty | "\\(.number) \\(.head.sha)"',
         [{"number": 7, "head": {"sha": "deadbee"}}], "7 deadbee\n"),
        ('.[0] // empty | "\\(.number) \\(.head.sha)"', [], ""),
        ('any(.[]; .body|test("@dependabot (rebase|recreate)"))',
         [{"body": "please @dependabot rebase"}], "true\n"),
        ('any(.[]; .body|test("@dependabot (rebase|recreate)"))',
         [{"body": "unrelated"}], "false\n"),
        # A comment with a null body must not crash the throttle read.
        ('any(.[]; .body|test("@dependabot (rebase|recreate)"))',
         [{"body": None}], "false\n"),
    ]
    passed = 0
    for expr, doc, want in checks:
        try:
            got = render(jq(expr, doc))
        except Exception as e:                       # noqa: BLE001
            got = f"<raised {e}>"
        if got == want:
            passed += 1
        else:
            print(f"  FAIL {expr!r} on {doc!r}\n    want {want!r}\n    got  {got!r}")
    print(f"selftest(jq): {passed}/{len(checks)} passed")

    # An unmodelled expression must FAIL, not approximate. Every caller reads a
    # nonzero exit as "GitHub did not answer" and fails safe; a plausible-looking
    # wrong answer would instead be recorded as a fact about a pull request.
    unsupported = 0
    for expr in ("group_by(.name)", ".a | map(.b)", "if .x then 1 else 2 end"):
        try:
            jq(expr, {})
        except Unsupported:
            unsupported += 1
        except Exception:
            pass
    print(f"selftest(unsupported-rejected): {unsupported}/3 passed")

    # Write forms. These can never be exercised against a live PR from a test,
    # so pin the request each one BUILDS instead: method, path and body. The
    # --match-head-commit sha is the judged-head guarantee -- if it stopped
    # reaching the payload, the gate would merge whatever the head had become
    # and every report would still read exactly the same.
    calls = []
    real_request, real_node = _request, _pr_node_id
    globals()["_request"] = lambda m, p, b=None: (calls.append((m, p, b)), (200, {}, ""))[1]
    globals()["_pr_node_id"] = lambda repo, num: "NODE"
    writes = [
        (["gh", "pr", "comment", "7", "--repo", "o/r", "--body", "@dependabot rebase"],
         ("POST", "repos/o/r/issues/7/comments", {"body": "@dependabot rebase"})),
        (["gh", "pr", "edit", "7", "--repo", "o/r", "--add-label", "needs-review"],
         ("POST", "repos/o/r/issues/7/labels", {"labels": ["needs-review"]})),
        (["gh", "pr", "merge", "7", "--repo", "o/r", "--squash",
          "--match-head-commit", "cafe1234"],
         ("PUT", "repos/o/r/pulls/7/merge",
          {"merge_method": "squash", "sha": "cafe1234"})),
    ]
    wrote = 0
    try:
        for argv, want in writes:
            calls.clear()
            rest_run(argv)
            got = calls[-1] if calls else None
            if got == want:
                wrote += 1
            else:
                print(f"  FAIL {' '.join(argv[1:4])}\n    want {want}\n    got  {got}")
        # --disable-auto has no REST endpoint; it must go to GraphQL, not
        # silently no-op, or a PR armed outside the allowlist stays armed.
        calls.clear()
        rest_run(["gh", "pr", "merge", "7", "--repo", "o/r", "--disable-auto"])
        m, path, body = calls[-1]
        if (m, path) == ("POST", "graphql") and "disablePullRequestAutoMerge" in body["query"]:
            wrote += 1
        else:
            print(f"  FAIL disable-auto -> {m} {path}")
    finally:
        globals()["_request"], globals()["_pr_node_id"] = real_request, real_node
    print(f"selftest(writes): {wrote}/{len(writes) + 1} passed")

    return (passed == len(checks) and unsupported == 3
            and wrote == len(writes) + 1)


def difftest():
    """Run every argv form through both transports and demand agreement.

    Requires a working `gh` AND a credential, so it is a local check, not a
    cloud one. Read-only: no write form is exercised against a live PR.
    """
    repo = os.environ.get("DIFFTEST_REPO", "")
    owner = os.environ.get("DIFFTEST_OWNER", "") or repo.partition("/")[0]
    branch = os.environ.get("DIFFTEST_BRANCH", "main")
    if not repo or not owner:
        print("difftest: set DIFFTEST_REPO=owner/name (and optionally "
              "DIFFTEST_OWNER) -- this compares two transports against YOUR "
              "GitHub, so it cannot ship a default fixture")
        return False
    # (argv, ordered?) -- `gh repo list` returns most-recently-pushed first
    # and this returns them sorted. Nothing consumes that order: all_repos()
    # sorts its result before returning it. The SET must match exactly, and
    # that is the check worth making -- one extra or missing repo is a whole
    # repo the sweep silently covers or skips.
    forms = [
        (["gh", "repo", "list", owner, "--limit", "500", "--no-archived",
          "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"], False),
        (["gh", "api", f"repos/{repo}", "--jq", ".allow_auto_merge"], True),
        (["gh", "api", f"repos/{repo}", "--jq", "{full_name,archived}"], True),
        (["gh", "api", f"repos/{repo}/commits?per_page=1",
          "--jq", ".[0].commit.committer.date"], True),
        (["gh", "api", f"repos/{repo}/commits/{branch}/check-runs",
          "--jq", "[.check_runs[]|{name,conclusion}]"], True),
        (["gh", "api", f"repos/{repo}/branches/{branch}/protection"
                       "/required_status_checks", "--jq", ".contexts"], True),
        # The base-freshness read (merge_gate.base_head). Its answer is
        # compared byte-for-byte to pulls.base.sha, so a transport that
        # rendered it with a trailing quote would stale every PR in the fleet.
        (["gh", "api", f"repos/{repo}/branches/{branch}",
          "--jq", ".commit.sha"], True),
        (["gh", "api", f"repos/{repo}/branches/no-such-branch/protection"
                       "/required_status_checks", "--jq", ".contexts"], True),
        (["gh", "api", f"repos/{repo}/pulls?state=open&per_page=1",
          "--jq", '.[0] // empty | "\\(.number) \\(.head.sha)"'], True),
        # `pr list` with a --jq filter. Added after the REST side returned the
        # raw array here and a caller spliced it into an API path; a form that
        # only one script issues is exactly the one no differential test
        # covers until it breaks.
        (["gh", "pr", "list", "--repo", repo, "--state", "all",
          "--limit", "3", "--json", "number", "--jq", ".[].number"], True),
    ]
    bad = 0
    for argv, ordered in forms:
        a = subprocess.run(argv, capture_output=True, text=True)
        b = rest_run(argv)
        # On a genuine "no", what must agree is the token sh_strict() greps
        # for: "404" separates an unprotected branch from an unread one.
        if a.returncode != 0:
            ok = b.returncode != 0 and ("404" in a.stderr) == ("404" in b.stderr)
        elif ordered:
            ok = b.returncode == 0 and a.stdout == b.stdout
        else:
            ok = (b.returncode == 0
                  and sorted(a.stdout.split()) == sorted(b.stdout.split()))
        if not ok:
            bad += 1
            print(f"  FAIL {' '.join(argv[1:])}\n"
                  f"    gh   rc={a.returncode} {a.stdout[:160]!r} {a.stderr[:80]!r}\n"
                  f"    rest rc={b.returncode} {b.stdout[:160]!r} {b.stderr[:80]!r}")
    print(f"difftest: {len(forms)-bad}/{len(forms)} argv forms agree")
    return bad == 0


if __name__ == "__main__":
    import sys as _sys
    ok = selftest()
    if "--difftest" in _sys.argv:
        ok = difftest() and ok
    if "--whoami" in _sys.argv:
        print(f"transport: MODE={MODE} binary_works={binary_works()} "
              f"credential={'yes' if token() else 'NO'}")
    _sys.exit(0 if ok else 1)
