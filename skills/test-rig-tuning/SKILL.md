---
name: test-rig-tuning
description: Use when standing up or tuning a rig that continuously exercises a tool whose failure destroys data, such as a churn rig, a soak test or a fault generator; covers finding which resource actually binds, proving the fault reproduces on new infrastructure before trusting any measurement, guard rails that fail safe, tuning for structural complexity rather than volume, and spotting parameters that silently undo each other.
---

# Tuning a rig that exercises a dangerous tool

Every step here exists because skipping it produces a rig that runs all night
and proves nothing. The order matters: each step is cheap and the one after it
is expensive.

## 1. Find the binding constraint before you build anything

It is almost never where you assume. Write down every resource the rig needs
and measure the headroom on each, then find the smallest one.

Storage, memory, throughput and time are separate ceilings and they do not
move together. A machine with terabytes of disk and not enough memory to hold
an index of it will produce a corpus you cannot process. Adding disk to that
machine buys nothing.

Two questions settle most of it. What is the cost per unit of whatever the rig
produces, in each resource? And which ceiling does that hit first?

Measure the cost per unit rather than estimating it. An estimate that is wrong
by 5x turns a comfortable plan into one that fills a disk at 3am.

> Seen once: an afternoon went into how to attach more disk to a cluster, on
> the assumption that a large corpus needs a large disk. Measuring said storage
> supported about 235M objects and memory about 20M. Memory bound first by more
> than ten to one, and every plan to add disk bought capacity nothing could
> process.

## 2. Split the work across machines by which resource it needs

Once you know the ceilings, components rarely all belong in one place. What
stores the corpus needs storage. What processes it needs memory. What generates
it needs throughput and should sit next to what it writes to.

These do not have to be the same host, and forcing them together caps the whole
rig at the weakest single machine.

## 3. Prove the fault exists on the new infrastructure

**Before any measurement.** A rig that does not reproduce the fault is a rig
measuring nothing, and you will not discover that until you are interpreting a
night of results.

Reproduce it end to end, capture the exact output, and keep it. Pin every
version that participates in the fault. A newer build that quietly fixed the
bug turns your generator into an ordinary healthy system, and the first sign
will be results that look better than they should.

If a component exists elsewhere in a known-good state, match its version
exactly rather than taking the current release. Two reference environments that
disagree will poison every comparison made afterwards, and the disagreement is
usually discovered long after the results are trusted.

> Seen once: the storage release under test rejects a call that newer releases
> accept, and that rejection IS the fault being studied. Deploying the current
> release on the second machine would have produced a healthy system that looked
> like a fixed bug, and the difference would have been blamed on scale.

## 4. Guard rails, before anything grows

Anything that accumulates needs a monitor with a hard stop, running before the
first byte lands.

**It has to fail safe.** A monitor that cannot read what it watches must stop
the run, not continue quietly. A watchdog that goes silent when blinded is
worse than none at all, because it is trusted.

Prove both paths before you rely on it: force the threshold breach and watch it
stop, then take away its ability to read and watch it stop again. A guard you
have not seen fire is decoration.

> Cheap to do: set the ceiling to 1 percent and confirm it stops, then point it
> at a target that does not exist and confirm it stops for that reason instead
> of falling silent.

Sample on a fixed interval, record the samples, and project from them. "Disk is
at 40%" is much less useful than "at the current rate it reaches the ceiling in
six hours", and the projection is what lets you act before you have to.

Watch every growth vector, not just the obvious one. A rig usually fills more
than one disk.

## 5. Tune for structure, not volume

This is where most rigs are wasted. A large simple corpus exercises less than a
small complicated one, and volume is the easier thing to produce, so that is
what people produce.

Ask what shape of input could make the tool give a wrong answer, then build
that shape. For anything reasoning about references and liveness, the hard case
is partial overlap: something referenced by several holders, losing them one at
a time, so its liveness depends on which survive. Inputs where every holder
references an identical set are the easy case, and they will pass whether the
tool is right or wrong.

Compose faults rather than injecting them one at a time. Most test suites apply
one defect per case; real counterexamples usually need two at once, and the
pair is what nobody has tried.

Run several generators with deliberately different shapes rather than several
copies of one. Identical generators multiply volume and add no structure.

> Seen once: the generator rolled its unit of work every 5 seconds while the
> thing that captured references ran every 2 minutes. Every unit reached its
> final state before anything captured it, so every capture held an identical
> set. Measured three deep with no evolution between them. Large, and trivially
> easy at the same time.

## 6. Find the coupled knobs

Two parameters that fire at the same moment are a trap. Any change to one
silently changes which regime you are in, and the rig keeps running while
measuring something else.

After setting the parameters, work out when each trigger actually fires under
the current rate. If two coincide, separate them deliberately: make one
effectively unreachable so the other governs alone. Then the remaining knobs
change magnitude rather than regime, and you can turn them without re-deriving
the whole design.

This is the step that catches "we turned the rate up and the results got
simpler", which otherwise looks like a discovery.

> Seen once: an age trigger of 30 minutes and a count trigger of 3.6 million
> units, at 2,000 units per second, are the same instant. Any increase in rate
> would have made the count fire first, shortening the window and producing a
> simpler structure. Raising the count trigger out of reach left the age one
> governing alone, and after that the rate changed magnitude instead of regime.

## 7. Verify the rig is doing the thing before trusting any output

A generator that has not started generating reads exactly like a system with
nothing wrong.

Check the specific event you depend on, by count, not by inference. If the rig
manufactures leaks by expiring things, confirm expiries have actually happened
and how many. A retention window longer than your observation window produces
zero events and a clean-looking result.

Make the event rate high enough to exercise the tool repeatedly within a short
window. Something happening a few times a minute finds flaws; something
happening every forty minutes finds nothing before you stop watching.

> Seen once: the first runs reported zero of the thing the rig exists to
> manufacture. The retention window was 40 minutes and every run had happened
> inside it, so nothing had expired yet. The counter that settled it was
> `retention_runs 16, expired 0`. A generator that has not started reads exactly
> like a healthy system.

## 8. Do not audit a moving target without saying so

A tool reading state that is being rewritten underneath it will behave
differently from one reading a settled snapshot, and a well-built tool will
refuse rather than guess.

Keep both: a live target for the behaviour under churn, and a quiesced one for
the honest measurement. Label every result with which it was. A number from a
moving target is not comparable to one from a still target, and mixing them is
how a regression hides.

> Seen once: audits against the live target explained 0 percent of the history,
> because the documents they needed were replaced between the listing and the
> fetch. The tool was refusing rather than guessing, which is correct, but it
> means a target under active churn cannot be usefully measured at all.

## 9. Record everything, including what passed

Raw output, unedited, one file per run, with the exact command and the exit
code. Committed somewhere durable as it is produced, not gathered at the end.

A passing result is evidence too. "These outputs matched byte for byte across
five settings" is worth nothing unless the outputs are on record for someone
else to check.

Write down what you expected next to what happened. In a week, a directory of
raw output with no statement of intent is unreadable.

## 10. Correct the record when the rig was wrong

Rig misconfiguration produces confident results. When you find that a run
measured the wrong thing, say so plainly in the record next to the number, and
say what the number actually meant.

Results collected before you understood the rig are not automatically wrong,
but they are not automatically comparable to what comes after either.

---

The lines marked "Seen once" are compressed from a single campaign. The full
version, with the measurements behind each, is in [README.md](README.md) beside
this file. They are here because a rule like "two
parameters that fire at the same moment are a trap" is easy to agree with and
hard to recognise in your own configuration.

A rig of that shape now ships in this repository as a single script,
[`snapshot_churn_rig.py`](../../snapshot_churn_rig.py), and the last
section of README.md maps each knob argued about here to the flag that sets it.
Reading the flags is the fastest way to see what a rig of this shape has to let
you turn.
