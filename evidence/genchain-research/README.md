# Research behind the generation-chain audit tool

Captured deliberately, because this project has already published thirteen
figures whose rig (the local test lab reproducing the fault, see
[FACTS.md](../../FACTS.md#the-test-lab-henceforth-the-rig)) was removed without keeping the artifacts. `evidence/README.md`
promises that a number in the documentation has its source in this directory.
This is that source for the work done on the replacement tool.

## What is here

`FACTS.md` is the consolidated record. Every statement in it was measured, or
read at Elasticsearch's source, during the investigation that produced the
replacement's design. It covers the on-disk format, the safety condition, the
known defects and the measured scale behaviour. It is the document a new
contributor should read first.

`harness/` measures where the tool breaks under size. `run_all.sh` reproduces
every number in `harness/REPORT.md`, and `results/` holds 22 result files.
`tool-snapshot` in the original run pinned the package so the numbers stayed
attached to one build. The harness is parameterised on sizes and failure rates,
so it can be pointed at a later version.

`reproducers/` holds three adversarial scripts from the review that falsified
the tool's central safety claim. Each imports the package directly, writes only
to its own scratch, and exits non-zero while any counterexample stands. They are
ordered: `REPRO.py` found six, `REPRO2.py` found three more after those were
fixed, `REPRO3.py` found four more after that. Read them in that order to see
how a fix aimed at one reproducer passes it without closing the class.

`ground-truth/` holds an independent oracle. Every delete was replayed on a
byte-identical filesystem mirror of the same repository state, where deletes
actually succeed, so a listing diff gives Elasticsearch's own deletion set by
name with no truncation. That is the only way established here to obtain the
complete set: the failed-delete log line caps at ten keys, and every
Elasticsearch API describing a snapshot reports counts rather than names.

## Why it is worth keeping

The numbers are cheap to lose and expensive to re-derive. Standing up the
environment, building the states, and running the campaigns took a working day.
Re-deriving the format facts means reading Elasticsearch's source again.

More importantly, the reproducers are regression tests for a class of defect
that has recurred: a live blob reaching a delete manifest. A fix verified only
against the reproducer that found it is verified against yesterday's bug.
