# Contributing

This project used to delete objects from production snapshot repositories. The
three tools that did it are retired and removed, and the conventions below exist
because of what they cost, not for their own sake. Nothing in the repository
deletes anything today, and the replacement being built has no delete path.

## Work from issues

Defects and feature requests live in [GitHub issues](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues),
not in handoff notes, scratch files, or a session transcript. If you find something
wrong, open an issue for it. If you fix something, say so on the issue.

Every issue carries three labels:

| Label | Meaning |
|---|---|
| `priority:P0` | Drop everything. Data loss, or the tool is unsafe to run. |
| `priority:P1` | Next up. Blocks a documented workflow or misleads an operator. |
| `priority:P2` | Real defect, workaround exists. |
| `priority:P3` | Backlog. |

| Label | Meaning |
|---|---|
| `criticality:critical` | Can destroy or lose data, or hide that data was lost. |
| `criticality:high` | Wrong result an operator would act on, or a safety gate that does not hold. |
| `criticality:moderate` | Incorrect but self-evident, or narrow blast radius. |
| `criticality:low` | Cosmetic, or affects only a rare path. |

Type is one of `type:defect`, `type:feature`, `type:docs`, `type:safety`. An issue
can carry more than one.

Priority and criticality are separate on purpose. A cosmetic problem on the page
every operator reads first can be P1 and low criticality. A data-loss path that
needs an unusual bucket layout can be critical and P2.

Agents working on this repo document what they did as a comment on the issue they
worked, including what they decided not to do and why.

### `blocked:owner-approval-required` means do not start

An issue carrying `blocked:owner-approval-required` records a direction that has
been considered and not approved. Do not implement it, in whole or in part, and do
not implement something adjacent to it that delivers the same capability under a
different name.

The gate opens one way only: the owner posts a comment on that issue saying to
proceed. Approval given anywhere else does not count. Not in a chat session, not
in a commit message, not inferred from a related decision, and not from an agent's
reading of what the owner probably wants.

The label exists because some of these ideas are reasonable, and a reasonable idea
is exactly the kind an agent talks itself into. If working on something else leads
you to conclude that a blocked issue is now obviously fine, that conclusion is the
thing the label is guarding against. Write your reasoning as a comment on the issue
and leave it closed.

### One worktree per issue

Every issue gets its own git worktree and its own branch. Work happens there, never
on a shared checkout, so two pieces of work cannot collide in a file and so the
diff for an issue is exactly the work for that issue.

```bash
git worktree add ../wt-issue-<N> -b issue-<N>-<short-slug>
```

**An issue is closed when its tree is pushed.** Not when the code is written, not
when the tests pass locally, not when an agent reports done. Pushed. Until then the
issue is open and the work is not real to anyone else.

**Name the issues a push closes, in the commit message, and close them.** Put
`Closes #N` in the commit body so the commit and the issue are linked for anyone
reading either one later. When the branch will not merge immediately, close the
issue explicitly with a comment saying what changed and how it was verified.

Never finish a push leaving an issue open that the push fixed. Work here is
issue-driven, so the tracker is how the next person decides what needs doing. An
issue that outlives its fix sends them to re-investigate something already solved,
and it buries the ones that genuinely need attention.

Close only what is genuinely done. Half fixed stays open with a comment naming the
remainder, because half fixed and closed makes the rest invisible.

Two things follow that are easy to get wrong.

The suite must be green in the worktree before the push, because green somewhere
else is not evidence about this branch. Several times in this project's history a
change looked correct in review and was structurally incapable of working, so
running it is the standard rather than reading it.

An agent working an issue works in that issue's worktree and nowhere else. If a
finding belongs to a different issue, it goes to that issue rather than being fixed
in passing, because a fix that arrives in an unrelated branch is a fix nobody can
review or revert on its own.

## Write prose with /humanize

All prose, code comments, and docstrings go through the `/humanize` skill before
they land. That covers documentation, issue bodies, issue comments, commit
messages, and comments inside code.

Code, identifiers, data, and numbers are never touched by a humanization pass.

The rules come from Wikipedia's "Signs of AI writing". The ones that come up most
here are: no em dashes or en dashes, no stock AI vocabulary, active voice with the
actor named, no shallow `-ing` tails bolted onto a fact, sentence case in headings,
and no comment that just restates the line below it.

After rewriting prose in a file that carries technical facts, check that no fact
moved. Numbers, byte offsets, format details, flag names, privilege strings, HTTP
status codes, file paths, and source citations all stay exactly as they were.

Safety warnings keep their full force. Smoother prose is never a reason to soften
a warning about deleting production data.

## Tests

### Test our code, not Python

A test earns its place only when it pins a decision we made. Ask whose bug the
test would catch. If the answer is a Python bug, a git bug, or a bug in a
third-party package, delete the test. Those projects test their own code.

Do not write tests for stdlib parsing, stdlib collection semantics, that `urllib`
performs a request, that a mock returns what the mock was told to return, or that
a library raises the exception its own documentation promises.

Do write tests for our classification rules and their fail-safe behaviour, our
reimplementation of formats Elasticsearch owns, our CLI contracts, our thresholds
and arithmetic, our request signing and percent-encoding, our retry and pagination
policy, our handling of secrets, and the shape of output another tool consumes.

### Every test says why it exists

Each test carries a comment naming what would break in the real world if that test
stopped passing. Name the consequence. The code already says what it asserts, so a
comment that restates the assertion is worse than no comment.

This is wrong, because it restates the code:

```python
def test_dry_run_deletes_nothing(self):
    # Runs the sweeper in dry-run mode and checks no files are deleted.
```

This is right, because it names the failure:

```python
def test_dry_run_deletes_nothing(self):
    # The whole safety model rests on --execute being the only path that
    # deletes. If a refactor ever makes the dry run fall through to the
    # delete branch, this is the test that catches it before a bucket does.
```

If writing that comment makes you reach for something like "covers the else
branch" or "checks the return type", delete the test instead of writing the
comment. Test count is not a goal. A smaller suite where every test means
something beats a large one nobody trusts.

### Use and abuse

Every behaviour gets a use case and an abuse case. The use case proves the thing
works. The abuse case proves the check can actually fail, which is what stops a
guard from passing vacuously forever. Each abuse case's comment says what abuse it
models and why that abuse is plausible here.

### Running them

```bash
python3 -m unittest discover -s tests -q
```

Standard library only, and no network. Both are deliberate. A suite that reaches
the network goes red for reasons nobody controls, and a suite that goes red for
reasons nobody controls gets skipped along with the findings that mattered.

## The tools stay single-file

`snapshot_sizes.py` is self-contained and runs from a copy on a jump host with
nothing installed. That was the rule when there were four scripts, code
duplicated between them was intentional, and it holds for whatever lands next.
Do not factor shared code into a module or turn this into a package.

## What the retirement leaves owed

Two debts are open and named here so nobody has to rediscover them.

The suite for `snapshot_sizes.py` was deleted with the sweeper suites even
though the tool itself stays. It is owed a fresh one. The old one had 131 of its
386 assertions doing nothing but checking that a particular sentence appears in
output, which is a suite that pins wording rather than behaviour, so it was not
worth carrying forward under the standard in this file.

`tests/fixtures/oci-signing-vectors.json` holds the known-answer vectors for OCI
request signing, extracted from the test file that was removed. There is no OCI
endpoint reachable from this project, so those vectors are the only offline
proof a signer can be checked against. Nothing reads them today. Whatever needs
to sign an OCI request next writes its own tests against them.
