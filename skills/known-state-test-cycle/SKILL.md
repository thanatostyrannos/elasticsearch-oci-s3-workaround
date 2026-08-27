---
name: known-state-test-cycle
description: Use when running a repeated-iteration test against a live rig, when asked for "N iterations" of anything, or before comparing two runs. Rebuilds the cluster and both object stores to a state you can name, runs the cycle, and emits one proof file covering every store exercised.
---

# Rebuild to a known state, then run the cycle

A hundred iterations against a rig that carried yesterday's state is not a
hundred iterations of one thing. It is one long drift, and every number it
produces describes a moment nobody can reconstruct.

## The rule

**When someone asks for N iterations, the first iteration starts from a state
you can name.** Not "the rig as we left it". A state you could rebuild from
scratch and get the same starting numbers.

Reset the cluster AND every object store the test touches. All of them, every
cycle.

## What it costs to skip this, measured

Run this project skipped it. The numbers below are what that produced.

| Symptom | What it actually was |
|---|---|
| Audit derives took over ten minutes | 645 accumulated root generations, none from the test |
| "Derives get slower each cycle" | True, and self-inflicted: nothing reaps a generation and nothing reset the repository |
| A cluster that went red mid-run | Debris from a QA session weeks earlier, on a volume that no longer existed |
| Two runs that could not be compared | Different starting object counts, different generation counts, different index sets |

The slowness was reported as a property of the tool. Some of it was a property
of never having cleaned up. That is the specific error this skill exists to
stop, and it is easy to make because a dirty rig runs fine, it just answers a
different question than the one you asked.

## What to reset

Everything the test reads or writes:

- **The cluster.** Delete it and bootstrap from the checked-in manifests, so
  the starting configuration is the one in version control rather than one
  assembled by hand across sessions. Ad-hoc changes made with `kubectl edit`
  do not survive, which is the point: if a setting matters, it belongs in the
  manifest.
- **Every object store prefix under test.** Both of them, if the test covers
  two. A run that resets one store and inherits the other produces two halves
  that cannot be compared, and the comparison is usually the whole reason for
  running against two stores.
- **Local state files.** A stale state file makes teardown refuse or, worse,
  makes it act on names from a previous run.

## What must NOT be reset

- **The fault.** The rig reproduces a bug on purpose. Do not "upgrade" the
  pinned object store to a release that fixed it. Losing the fault is losing
  the rig.
- **Anything outside the test's own prefix.** Namespace everything the run
  creates and delete only what matches. A reset that reaches past its prefix
  is a reset that destroys somebody else's work.

## The order, and why it is an order

1. **Stop the writers first.** Load generators, snapshot policies, harnesses.
   Tearing down underneath a running writer leaves objects written after the
   purge, so the "known state" is already wrong before iteration one.
2. **Tear down inside the cluster**, using the tool that knows the names.
3. **Purge the store prefixes**, and only then, because a purge before the
   cluster stops writing is a purge that races.
4. **Delete and rebuild the cluster** from manifests.
5. **Prove the reset**, before running anything.
6. **Then start the cycle.**

## Prove the reset. Do not infer it.

A generator that has not started reads exactly like a system with nothing
wrong, and an empty result reads exactly like a clean one. Before iteration
one, record and check:

- Object count under each prefix. It should be zero, and you should have
  counted it rather than assumed the purge worked.
- Root generation count, which should be at its floor rather than in the
  hundreds.
- Index and data-stream count for the prefix, which should be zero.
- That the fault still reproduces. On a store with the batch-delete bug this
  shows up at registration; capture the rejection. A rig that has quietly
  stopped reproducing the fault will pass every test for the wrong reason.

Anchor every count. A loose substring match over tool output produced a wrong
scoreboard four times on this project, and each time the underlying execution
output was right.

## One proof file per build, covering every store

The point of running against two stores is comparison, so the proof is one
file, not two. Write it to `docs/run-proofs/<version>.md` so it ships with the
release it describes.

    # Run proof: <version>

    Build: <commit>, payload digest <digest from MANIFEST.sha256>
    Date: <when>

    ## Starting state, proven not assumed
    | Store | Objects before | Root generations | Indices | Fault reproduced |

    ## Configuration
    Both stores, side by side. Anything that differs between them is a
    confound and should be named as one.

    ## Cycles
    | Store | Cycles | Shard dirs read | Segments condemned | Deleted | Failed | Unconfirmed |

    Totals read from the per-cycle execution files with anchored matches,
    never from a summary file the harness maintains.

    ## Nothing was destroyed
    Restore results with document counts, and integrity checks. A check that
    did not run is not a check that passed, and it is recorded as not run.

    ## Ending state
    | Store | Objects after | Left behind | Teardown verified |

    ## What this does and does not show
    Rates are properties of the rig's cadence and do not transfer. Counts and
    orderings do. Say which is which.

Then run the prose through the `humanize` skill before saving it. A proof
nobody reads is not proof, and scan-and-report output drifts into the flat
register that makes people skim.

## When a run fails

A harness that stops because a cycle reported failed or unconfirmed deletes is
working. That is the safety stop, and it means the repository had a problem.

A harness that needs its steps changed to run at all has failed, and that is a
different thing. Do not patch around it and continue: the run is measuring a
procedure nobody will repeat. Stop, fix the harness, reset to a known state
again, and restart the cycle from iteration one. A fixed harness resuming into
a dirty rig inherits both problems.
