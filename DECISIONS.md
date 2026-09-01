# Decisions

Every rule in this repository is a postmortem. This file records what
happened, what the rule is, and why the obvious alternative was tried first
and failed. Dates and PR numbers are real; the repos are the author's.

The one-sentence version: **agent judgement drifts; computation doesn't.**
Anywhere a person or an agent was asked to *look* at a PR and decide, the
decision eventually went wrong in a way nobody noticed. Anywhere the decision
was a function of data the API returns, it stayed right — or failed loudly.

---

## I. What counts as a safe shape

### 1. Shape is computed from changed paths. Never from the title, branch, label, or author's claim.

The first version of the review loop asked an agent to "enable auto-merge when
the PR is green and the shape is eligible", and let the agent judge shape by
reading the PR. It armed seven PRs the allowlist forbids, including a 197-file
diff and an 82-file diff, and re-armed a ten-file `mixed` PR two days after a
human had disarmed it by hand. `ci: minute-safe Actions policy` reads exactly
like a CI fix. It rewrote workflows across the fleet and deleted a Dependabot
ecosystem in one repo.

`classify()` takes the list of changed file paths from the API and nothing
else. A loop that mislabels its own output cannot talk its way past the gate,
because the gate never listens.

### 2. There is no partial credit.

If any changed path falls outside the matched shape's patterns, the shape is
`mixed`, and `mixed` always routes to a human. A PR that adds forty tests and
edits one source file is not "mostly tests"; it is a source change with tests.
The 197-file hardening PR above touched one blog post, and under a majority
rule would have been `content`.

### 3. The content veto is separate from shape, and absolute.

Editorial content is a human judgement call, so a changed path under
`content/`, `posts/`, `blog/` and friends blocks auto-merge regardless of what
else is in the diff. Keeping this out of `classify()` means a dashboard can
label the 197-file PR honestly as `mixed` without the safety property living
in the label. And an *unreadable* diff vetoes: "I could not check for content"
must never resolve to "there is no content".

### 4. `ci-fix` is default deny, and the allowlist had a bug that meant it had never fired.

A workflow-only diff qualifies only if every added line matches an explicitly
reviewed regex of action bumps. Adding to that list is a deliberate act.

While auditing this, it turned out `ci_fix_allowed()` — the function that
bridges `ci-fix-candidate` to `ci-fix` — had no callers anywhere in the repo.
The whitelist documented for weeks had never let a single PR through. The
gate was safe by accident. `promote_candidate()` now exists precisely to be
the one place a candidate shape earns its final name.

### 5. `data-artifact-only` is opt-in per repo, and absence means deny.

Scraper output and regenerated datasets are safe to merge blind only in repos
that have said so. `data_dir` in `repos.yml` is that statement. A repo that
has never declared one cannot produce the shape — an unset field is not an
empty allowlist, it is a closed door.

---

## II. What a version delta means

### 6. Deltas come from the PR **body**, and a group is as risky as its worst member.

A Dependabot group PR is titled *"bump the development-dependencies group
across 1 directory with 21 updates"*. That names no versions. Its body
tabulates all 21, including `@testing-library/jest-dom` 6→7 and `jsdom`
28→30. `parse_bump_deltas()` reads the table and the per-package lines;
`bump_kind()` takes the **maximum** severity, never the average and never the
first row. One patch and one minor score `minor` — proving it is the max —
and one hidden major blocks the whole group.

### 7. `requirements*.txt` is a manifest, not a lockfile.

It is where a Python dependency is *declared*. Counting it as a lockfile let
every Python bump skip the version rule entirely: pytest 7.4.4→9.1.1, mypy
1.8→2.2, kafka-python 2→3 and cryptography 48→50 all classified
`lockfile-only` and were eligible to merge with no version check at all. It
now sits with `package.json` and `pyproject.toml`, so patch and minor still
merge and majors route to a human — the rule every other ecosystem already
got.

(The comment on that function also promised `requirements/base.txt` — the
pip-tools directory layout — was covered. It was not: the check looked only
at the basename, so `base.txt` never matched. It failed closed, but the code
disagreed with its own comment for weeks. Fixed 2026-08-28, with a test.)

### 8. `lockfile-only` still gets the version rule.

One PR touched nothing but `uv.lock` — so `lockfile-only` is the honest
shape — and moved cryptography from 48.0.1 to 50.0.0. It armed for auto-merge
with the version rule never consulted. The lockfile is the dependency set
that ships; "the manifest didn't change" describes how the range was
*written*, not how far the code *moved*. `promote_candidate()` demotes a
lockfile-only PR to `mixed` when it carries a delta outside the allowed
kinds. A loop-produced "chore: refresh lockfile" carries no from/to at all,
so its long-standing behaviour is untouched.

### 9. `0.x` minors are majors, SHA bumps are unanswerable, and both constants stay as they are.

Under semver a `0.x` release has no stability guarantee: 0.27→0.41 may break
exactly as hard as 1.0→2.0. `ZERO_MAJOR_MINOR_IS_BREAKING=True` is the strict
reading, and it is the right default when the gate downstream may be a
linter. A SHA-pinned action bump carries no version information at all, so
"patch or minor?" is *unanswerable* rather than false, and unanswerable
routes to a human (`TRUST_SHA_PINNED_ACTION_BUMPS=False`).

These two constants have a consequence worth stating because it refutes the
obvious next idea. The obvious fix for a 177-PR bot queue is to group
patch+minor into one auto-mergeable PR per ecosystem. It does not work here,
because `bump_kind()` takes the max: one `0.x` minor or one SHA bump inside a
group demotes the whole PR to `mixed`, so a group of thirty safe bumps never
merges — strictly worse than thirty singles that at least land one at a time.
Measured 2026-08-15: 60 of 781 npm dependencies across 20 repos were `0.x`,
and the cleanest candidate repo had five `0.x` pins *and* SHA-pinned actions.
Grouping only pays after one constant is deliberately relaxed. That is a risk
decision, not a cleanup, and it lives on one line where someone has to mean
it. Re-examined 2026-08-28 and left alone: the same queue was 104 majors out
of 177, so the constants are not what is holding it.

### 10. Ranges are not versions, and the sentence's period is not part of the number.

`Update pytest requirement from <8.0.0,>=7.4.4 to >=9` changes a
*constraint*, not a pin. Coercing `>=9` to `9.0.0` and `<8.0.0,>=7.4.4` to
`8.0.0` would score it as a confident 8→9 major — a guess wearing a number.
`_semver()` returns `None` on anything containing a range operator, and
`None` means `unknown`, which routes to a human.

And a lazy regex on *"from 3.1.2 to 3.1.5."* captured `3` as the new
version — the sentence's terminating period ended the match — turning a
patch into a fake minor. Greedy, then strip the period. There is a test.

---

## III. What "green" means

### 11. Three conditions, all required. An ungated repo never merges.

Condition 2 — *the repo has at least one required status check* — was
missing at first, which is how `dotfiles#41` merged with "no checks
reported". A shape match on a repo with no gate is not evidence of anything;
it routes to review like everything else. Separately, the repo's own
`allow_auto_merge` setting is respected as an operator decision: a repo with
only a security scan as its required check has been switched off deliberately,
because a security scan is not a correctness gate.

### 12. Required checks are sampled from a PR head commit, never from the default branch.

Check names differ between `push` and `pull_request` events. `cocoon` runs
`ci-tests / ci-tests-minimum` on push and `quality` on PRs. Requiring the
push-side name creates a check that never fires on a PR, so it never goes
green, so no PR can ever merge — permanently, silently. `TAlker` sat in
exactly that state. The trap was reproduced on seven repos in one evening
before `verify-gates.py` was written to catch it, and then reproduced *again*
by the first cut of `ci_watchdog.py`, which sampled the default branch and
declared two healthy repos "2/2 required RED" and "5/5 RED". Both scripts now
read check-runs from an open PR's head SHA, and report *unverifiable* when
there is no PR to sample rather than guessing.

### 13. Check names contain commas.

`"Lint, Test & Build"` is one context. Joining and splitting the required
contexts on `,` shreds it into three phantom checks that can never be
satisfied. The JSON array is parsed as an array, everywhere.

### 14. Never arm. Merge exactly the judged head, or nothing.

On 2026-08-17 the gate judged `finance-owl#72` — nodemailer 8.0.2→8.0.5, a
minor, allowlisted — and enabled auto-merge. Dependabot force-pushed
**9.0.1**, a major, into the same PR at 02:21:49. Auto-merge landed it at
02:23:59. The version rule never saw what shipped. 130 seconds; no sweep
cadence polices that window, because auto-merge survives force-pushes by
design.

An arm is standing permission to merge *future* content on the strength of a
judgement about *past* content. The gate now merges a green PR immediately
with `--match-head-commit <judged sha>`, which makes GitHub refuse
server-side if the head moved between the read and the merge, and it
*disables* any arm it finds on any PR — declining to arm was never enough,
because a previously-armed PR lands on its own.

Proof it works, eleven days later: on 2026-08-28 the gate judged
`cocoon#88` at eslint 10.9.0, Dependabot rewrote it to 10.9.1 mid-sweep, and
the merge was **refused**. The next sweep judged the new head and merged it.
A pending PR waits one sweep; that latency is the price of never merging
unjudged content.

### 15. Drafts never merge.

A draft cannot merge and cannot be armed, whatever its checks say. 22 of 56
open PRs sat in that state while the review loop reported them
"awaiting-review" for weeks — they were never eligible for review to matter.
Nothing in the loop prompts asked for drafts; the agents did it on their
own. The gate files a draft under *skipped*, and the loop conventions now
forbid opening one.

### 16. Only machine-produced PRs are considered, recognised by label **or** branch.

Work a human opened is never auto-merged, whatever its diff. Machine output
is recognised by the `automated` label, by an `auto/<loop>-<date>` head
branch, or by being Dependabot's. The label alone was not enough — it was
applied inconsistently and missed roughly 44% of loop output — so the
deterministic branch name the loop conventions mandate is the reliable
marker, and either is accepted.

### 17. Only `dirty` is a conflict, and only Dependabot's conflicts self-heal.

GitHub's `mergeable_state` is `unknown` while it is still computing, and
`unknown` is not a verdict. Only `dirty` counts. When a Dependabot PR is
dirty, the gate asks it to rebase with the `@dependabot rebase` comment —
five armed bumps once sat green-but-dirty for days over one shared lockfile
until someone did that by hand. The request is throttled to once per 24h
using the PR's own comment timeline as the state store: it is the only store
every runner shares, and the comment *is* the action, so the record cannot
drift from reality. Loop-produced PRs get no kick, because only their loop
can rewrite them.

---

## IV. Reading GitHub honestly

### 18. Enumerate per repo. Never search.

`gh search prs --limit N` returns exactly N rows and says nothing when there
are more. No error, no warning, no flag. The gate asked for 100 and got 100
of 407 open PRs, reasoning about a quarter of the queue while reporting
totals as if complete. Raising the limit does not help: the Search API
hard-caps at 1000 across all pages. The branch-audit script had the same
bug, saw 200 of 482, and queued **144 branches with live open PRs** for
deletion; only a dry run caught it.

`gh pr list --repo X` reads the repo's own index, paginates honestly, and has
no fleet-wide cap. A repo hitting the per-repo limit raises rather than
undercounts, and the Search API's `total_count` — exact even when its pages
are capped — is used as an oracle to confirm the enumeration saw everything.
The general rule outlives `gh`: **if a bounded query can return exactly its
bound, treat that as failure until proven otherwise.**

### 19. Owners are a list, and an owner that yields nothing is an error.

The fleet spans a personal account and an org. A single owner string meant
`gh repo list` never returned the org's repos, so they were not *declined* by
the policy — they were *invisible* to it, and the gate reported a drained
queue that had never included them. `verify-gates.py` had the identical bug
against the identical org and had never actually checked them. Every owner is
enumerated; an owner returning zero repos aborts the sweep, because a typo, a
rename and a dropped token scope all present as "no repos".

### 20. Dependabot's login differs between `gh` subcommands.

`gh search prs` reports the author as `dependabot`; `gh pr list` reports the
same author as `app/dependabot`. A `startswith("dependabot")` check matched
every bot PR under one command and none under the other, and silently
returned 0 of 339. Substring match, with the `is_bot` flag keeping a human
called `dependabot-fan` out of the bot bucket.

### 21. `None` is not `[]`. Unreadable is not empty.

`changed_paths()` used to return `[]` on any failure, so a 404 — one repo's
remote has been gone since 2026-08-03 — or a transient rate-limit came back
as a clean `empty` shape: the gate recording "there is nothing in this diff"
when the truth was "I could not look". It was safe only because `empty` is
not on the allowlist. That is luck, not design. `None` is now its own shape,
`unreadable`, and it vetoes.

### 22. The absence of an answer is never rendered as one.

Every skip reason the gate prints is a claim: "repo has auto-merge disabled"
asserts an operator decision; "no required checks on main" asserts the repo
is ungated. Both were being printed whenever a TLS handshake blipped, because
the helper returned a nonzero code and each caller read that as the negative
answer. Successive runs disagreed with each other — armed went 9→6→5 in ten
minutes with nothing changing on GitHub — and one PR was reported as living
on an unprotected branch one run after the gate had listed its five green
required checks.

Failing closed made none of that unsafe. It made the report untrustworthy,
which is worse than useless. `sh_strict()` returns `None` only on a genuine
404 and raises `ReadFailed` on anything else; `main()` files those PRs under
*NO VERDICT — GitHub did not answer* and says the run is incomplete. A reader
can now tell a repo that needs gating from a socket that needed retrying.

### 23. `--paginate` with `--jq '[...]'` emits one array per page.

The concatenation is not valid JSON. Emit one value per line and split.

### 24. Bots are in by default.

Dependabot was opt-in (`INCLUDE_BOTS=1`) when first added, out of caution.
The caution was the bug. Dependabot opens roughly four fifths of an actionable
queue, and every run that forgot the flag reported a drained queue — the
operating notes literally said so, and it happened again on 2026-08-28: the
default run reported 19 PRs; the correct run reported 137, thirteen of them
safe to merge immediately. Nothing about the policy is relaxed for bots. They
were simply being ignored. Flipped that night; `INCLUDE_BOTS=0` opts out.

---

## V. The watchdog

### 25. A red default branch is upstream of everything, and nothing else can see it.

`album-conceptualizer`'s CI broke on 2026-07-10. Every PR opened afterwards
failed its required checks, so nothing could merge; 15 policy-clean Dependabot
PRs piled up and 5 more were orphaned into permanently-unrebaseable zombies.
Nothing reported it for **37 days**, because a repo with a dead pipeline and a
repo with nothing to do look identical from outside: both are quiet. The
merge gate can only ever say "required check red" one PR at a time; it has no
memory and no notion of the default branch's own health.

`ci_watchdog.py` measures the default branch directly, and every filter in it
is a false positive that shipped:

- **Severity is set by consequence, not noise.** A red workflow nothing
  gates on blocks no merge; half the fleet's release workflows have been
  `startup_failure` forever. Only a *required* check red at an open PR's
  head escalates. Everything else is a NOTE.
- **Judged per workflow, not per run.** One red workflow out of four is a
  different finding from four out of four; collapsing them hides the case
  that matters. The headline is the workflow that has gone longest without
  a green run, because that is the window in which nothing could merge.
- **`event=push` and `branch=<default>` — as query parameters.** PR-event
  runs come from head commits, so one broken feature branch masquerades as
  a broken repo. Dependabot's update jobs run as `dynamic` on the default
  branch, and the first cut read "npm_and_yarn in /apps/web" as the repo's
  CI and called a healthy repo BROKEN. And the filters must bound the window
  *before* fetching: one repo's newest 100 runs were 97 scheduled jobs from
  a single day, zero rows survived a post-hoc filter, and a repo with 302
  real CI runs was filed as "runs no CI at all". Five repos got that verdict.
- **SILENT only when commits landed and CI did not fire.** "No runs lately"
  is only a finding if there was something to run on. An earlier cut flagged
  17 repos, every one either idle or deliberately configured that way.
- **Exit 1 only on a measured blocker**, so a routine can gate on it without
  being woken by a release workflow nobody merges against.

---

## VI. Reaching GitHub at all

### 26. The gate must run where there is no usable `gh` binary.

Every script here shells out to `gh`. A scheduled runner operating this policy
could not authenticate it, and for three weeks reported *"GitHub access is not
enabled for this session"* on 15+ consecutive runs and skipped. The gate had
not declined to arm anything — it never executed, and every report of a
drained queue described a sweep that had not happened. Producing loops
meanwhile throttled themselves correctly against a PR cap only the gate could
relieve.

That runner was not cut off from GitHub: it pushed commits on every run, so a
credential was reachable, just not through `gh`. `gh_transport.run(argv)` takes
the same argv the scripts already build and issues it over REST when the binary
cannot answer. Credentials come from `GH_TOKEN`/`GITHUB_TOKEN`, then
`gh auth token`, then `git credential fill` — the last being the same question
git asks before a push, so *if the push works, the reads work*.

Call sites are unchanged deliberately. Those argv strings encode detail that is
easy to lose by hand: `--paginate` with a line-per-record jq (§23),
`--match-head-commit` (§14), gh's uppercase `visibility`. Routing them beats
rewriting them.

### 27. An unmodelled argv form fails. It does not approximate — and once it did.

Only the forms this package issues are modelled. Anything else returns a
nonzero code, because every caller already reads that as "GitHub did not
answer" and fails safe (§22). A plausible wrong answer would instead be filed
as a fact about a pull request.

`_cmd_pr_list` broke that promise from the day it was written. It parsed
`--jq`'s neighbours and then ignored the flag, returning the raw projection
where `gh` yields one value per line:

    URL can't contain control characters.
    '/repos/o/r/pulls/[{"number": 98}, {"number": 97}]'

The caller had spliced the array into an API path. A crash was the *lucky*
outcome: the same value reaching a script that samples PRs before writing
branch protection yields "no PRs to sample" and silently under-enforces the
gate it was run to enforce.

`_cmd_api` and `_cmd_repo_list` had always honoured `--jq`; `pr list` was the
gap. Nothing in this package issues that form — only two scripts in the
consuming control plane do — which is exactly why an eight-form differential
test never covered it. **The form only one or two callers issue is the one the
differential test will not cover until it breaks.** It is form #9 now, plus a
pure regression test that needs no network.

Two `gh` behaviours are matched deliberately, because the differential test is
worthless without them: gojq sorts object keys where CPython's `json`
preserves insertion order, and `gh repo list` omits `disabled` repos — letting
one through would add a repo to the sweep that can only ever 403, the same
reason archived repos are excluded (§18).

### 28. `repos.yml` is not "next to the source file".

`REPOS_YML` was `<this file>/../repos.yml`. Correct for a script sitting in a
control plane's `scripts/` directory, meaningless once installed, where it
resolves to `site-packages/../repos.yml` and never exists.

It failed quietly in the direction that matters. `load_data_dirs()` swallows a
missing file and returns `{}` on purpose (§5: absence means deny), so every
`data_dir` opt-in would silently stop counting and `data-artifact-only` would
silently stop matching, with nothing in any report saying so. Only
`parse_repos_yml()` — deliberately strict — failed loudly, and that is how it
was found, on the first fresh-venv run of a consumer.

`GATE_REPOS_YML`, else `repos.yml` in the working directory, **resolved per
call** rather than bound at import, so a consumer can point at its own file
after importing the package. An absolute path is the safe form: the working
directory is right for an operator standing in their checkout and wrong for a
loop that `cd`'d into a clone it was fixing.

---

## VII. What was deliberately not done

- **The private control plane vendored these files until 2026-08-31.** The
  two copies were "kept in sync by `diff`", and nobody ran the diff:
  `classify_pr.py` had drifted 127 lines, and the transport in §26 existed
  only in an unmerged PR *there*. None of the drift was policy — it was owner
  wiring, cross-references and usage strings — which is the argument for a
  dependency rather than for better discipline. It consumes this package now,
  and both bugs in §27 and §28 were found by that consumer's first
  fresh-venv run, having been invisible for as long as the caller stood in a
  checkout with a working `gh`.
- **The live fixtures are empty here.** They ran against the author's PRs
  and belong to whoever operates the gate. The pure self-tests always run.
- **No branch protection on this repository.** It is single-author; the CI
  check is evidence, not a gate.
- **The two policy constants stay strict** (see §9).
