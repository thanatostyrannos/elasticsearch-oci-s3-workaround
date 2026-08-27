"""The command line: naming a transport, refusing to guess one, and output.

An unanswered question is a refusal in this project rather than a pass, and
choosing a transport quietly is the same class of mistake as choosing a scope
quietly. The prompt here is a choice rather than a confirmation, so it is
deliberately not the typed-confirmation gate the sweepers put in front of a
delete: this tool deletes nothing, and teaching an operator that typing at a
prompt here is routine would blunt the gate that matters.

Nothing below asserts a sentence. A reworded warning must not turn a suite
red, so each test pins the fact an operator acts on: the exit code, the
transport that ran, the columns another tool consumes, the counts.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import cli
from generation_chain.reporting import manifest
from generation_chain.sources import s3

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


class _Answers(io.StringIO):
    """A stdin that is not a terminal unless a test says it is."""

    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class CommandLine(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-cli-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.coverage = os.path.join(self.dir, "coverage.json")

    def run_cli(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(argv + ["--coverage-json", self.coverage],
                        stdin=stdin or _Answers(), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def report(self):
        with open(self.coverage, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_named_transport_runs_without_asking_anything(self):
        # Scripted and repeated use has to keep working. A tool that prompted
        # even once when the operator had already said which store to read
        # would break every cron entry and every rehearsal script.
        code, out, _err = self.run_cli(["--local-repo", self.root])
        self.assertEqual(code, 0)
        self.assertEqual(self.report()["transport"], "local")
        self.assertIn("indices/iuuid-idx/0/__a",
                      [line.split("\t")[0] for line in out.splitlines()])

    def test_no_transport_and_no_terminal_refuses_rather_than_guessing(self):
        # Abuse case, and the one that keeps this usable from CI. Silence must
        # never pick a transport, and the refusal has to name the flags or the
        # operator cannot fix the invocation from what they were told.
        code, out, err = self.run_cli([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(out, "")
        for flag in ("--transport", "--local-repo"):
            self.assertIn(flag, err)

    def test_the_prompt_decides_the_transport_and_offers_all_three(self):
        # The choice the owner asked for: three real options, no default, and
        # no inference from whatever other arguments happen to be present.
        code, _out, _err = self.run_cli(
            [], stdin=_Answers(f"3\n{self.root}\n", tty=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.report()["transport"], "local")
        self.assertEqual(sorted(cli.TRANSPORTS), ["local", "oci", "s3"])

    def test_choosing_s3_without_an_endpoint_offers_both_oracle_domains(self):
        # This is the specific trap. Oracle publishes a standard domain and a
        # dedicated one, and the wrong choice fails in a way that reads like a
        # network or a credential problem, so an operator burns an hour on the
        # wrong diagnosis. Both forms have to be on screen, and this tool must
        # not construct one for them.
        prompt = io.StringIO()
        chosen = cli.choose_s3_endpoint(
            _Answers("1\nmynamespace\nus-ashburn-1\n", tty=True), prompt)
        self.assertIn(s3.STANDARD_ORACLE_ENDPOINT, prompt.getvalue())
        self.assertIn(s3.DEDICATED_ORACLE_ENDPOINT, prompt.getvalue())
        self.assertEqual(
            chosen,
            "https://mynamespace.compat.objectstorage.us-ashburn-1"
            ".oraclecloud.com")

    def test_the_manifest_columns_are_the_contract_another_tool_reads(self):
        # The comparison against the reachability sweeper is external, with
        # comm over cut -f1. A column order change breaks that silently and
        # produces a differential between two unrelated things.
        path = os.path.join(self.dir, "orphans.tsv")
        code, _out, _err = self.run_cli(
            ["--local-repo", self.root, "--manifest", path])
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0].split("\t"), list(manifest.MANIFEST_COLUMNS))
        self.assertEqual(manifest.MANIFEST_COLUMNS[0], "key")

    def test_a_refused_run_exits_non_zero_and_writes_only_a_header(self):
        # An empty manifest and a clean repository look identical. The exit
        # code and the coverage record are the only things that separate
        # "nothing to clean up" from "I could not read this at all".
        fx.corrupt(self.root, "index.latest", b"short")
        path = os.path.join(self.dir, "orphans.tsv")
        code, _out, _err = self.run_cli(
            ["--local-repo", self.root, "--manifest", path])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIsNotNone(self.report()["refused"])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

    def test_the_coverage_record_counts_the_history_it_could_not_see(self):
        # An operator must never read "I could not see it" as "it is fine". A
        # missing generation is history this tool cannot explain, and the keys
        # that history would have named are silently absent from the manifest,
        # so the gap has to be counted where a script can find it.
        fx.remove(self.root, "index-1")
        code, _out, _err = self.run_cli(["--local-repo", self.root])
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual(report["generations_missing"], [1])
        self.assertEqual(report["transitions_total"], 2)
        self.assertEqual(report["transitions_explained"], 0)

    def test_the_self_test_proves_the_signing_with_no_network(self):
        # A signing mistake fails as a bare 403 or 401 that names nothing, so
        # an operator on a jump host needs a way to find it at their desk.
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["--self-test"], stdin=_Answers(),
                                  stdout=out, stderr=err), 0)

    def test_every_key_reaches_the_classification_with_a_disposition(self):
        # The categories are the point: an operator has to tell a key this
        # tool withholds as evidence from one it genuinely cannot explain, and
        # a flat orphan list cannot say which is which.
        path = os.path.join(self.dir, "classified.tsv")
        code, _out, _err = self.run_cli(
            ["--local-repo", self.root, "--classification", path])
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as fh:
            rows = [line.split("\t") for line in fh.read().splitlines()[1:]]
        placed = {row[0]: row[1] for row in rows}
        self.assertEqual(sorted(placed), fx.read_keys(self.root))
        self.assertEqual(placed["index-0"], "evidence")
        self.assertEqual(placed["index-2"], "live")
        self.assertEqual(placed["indices/iuuid-idx/0/__a"], "orphaned")

    def test_an_endpoint_path_missing_a_field_refuses_rather_than_guessing(self):
        # A wrong region and a wrong endpoint both answer a bare 403 that
        # names no component, so neither is ever defaulted. An operator who
        # gets a refusal here fixes the invocation; one who gets a 403 spends
        # the afternoon on credentials that were fine.
        for argv in (["--transport", "s3", "--endpoint", "http://x",
                      "--bucket", "b"],
                     ["--transport", "oci", "--bucket", "b",
                      "--oci-region", "us-ashburn-1"],
                     ["--transport", "oci", "--namespace", "n", "--bucket", "b"]):
            code, out, _err = self.run_cli(argv)
            self.assertEqual(code, cli.EXIT_USAGE, argv)
            self.assertEqual(out, "")

    def test_credentials_are_never_taken_from_the_command_line(self):
        # An access key in argv reaches the process table and the shell
        # history of every operator on the box. The environment is the only
        # place this tool reads one from.
        parser = cli.build_parser()
        flags = {action.option_strings[0] for action in parser._actions
                 if action.option_strings}
        self.assertNotIn("--secret-key", flags)
        self.assertNotIn("--access-key", flags)


if __name__ == "__main__":
    unittest.main()


class ExitCodes(unittest.TestCase):
    """The codes are a contract, so they are pinned and they are documented.

    A caller derives success from the exit code, and the right response to a
    store that did not answer is the opposite of the right response to a
    repository whose format is unsupported: retry the first, and never the
    second. The retired tools in this repository defined five codes and their
    documentation named none of them, which makes a code nobody can rely on.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-exit-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def run_cli(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(argv, stdin=stdin or _Answers(), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_each_outcome_carries_its_own_code(self):
        # Four distinct outcomes, four codes. Collapsing the transport failure
        # into the refusal would tell a caller to give up on a store that was
        # briefly busy; collapsing the refusal into the transport failure
        # would have it retry an unsupported format forever.
        self.assertEqual(self.run_cli(["--local-repo", self.root])[0],
                         cli.EXIT_OK)
        self.assertEqual(self.run_cli(["--local-repo", "/nonexistent"])[0],
                         cli.EXIT_TRANSPORT)
        self.assertEqual(self.run_cli([])[0], cli.EXIT_USAGE)
        fx.corrupt(self.root, "index-2", b'{"min_version": "6.8.0"}')
        self.assertEqual(self.run_cli(["--local-repo", self.root])[0],
                         cli.EXIT_REFUSED)

    def test_every_code_the_tool_returns_is_in_the_help_output(self):
        # An undocumented exit code is a contract nobody can rely on. This
        # pins the documentation to the constants rather than to prose, so a
        # new code cannot ship without a line describing it.
        help_text = cli.build_parser().format_help()
        for code in (cli.EXIT_OK, cli.EXIT_REFUSED, cli.EXIT_USAGE,
                     cli.EXIT_TRANSPORT):
            self.assertIn(f"  {code}  ", cli.EXIT_CODES)
        self.assertIn("Exit codes", help_text)

    def test_a_run_interrupted_part_way_leaves_no_partial_manifest(self):
        # A manifest is a list an operator acts on, and a truncated one reads
        # as a complete, shorter list with nothing in it saying otherwise.
        path = os.path.join(self.dir, "orphans.tsv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("previous contents\n")

        def explode(_handle):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cli._write_atomically(path, explode)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "previous contents\n")
        self.assertEqual([n for n in os.listdir(self.dir)
                          if n.startswith(".genchain-")], [])

    def test_a_finished_manifest_file_carries_the_completion_marker(self):
        # The property the marker exists for: open the file on its own,
        # without the coverage record beside it, and still be able to tell
        # it describes the whole repository.
        path = os.path.join(self.dir, "orphans.tsv")
        code, _out, _err = self.run_cli(
            ["--local-repo", self.root, "--manifest", path])
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines()[-1],
                             manifest.COMPLETION_MARKER.rstrip("\n"))

    def test_a_refused_runs_manifest_file_carries_no_completion_marker(self):
        # The other half. A reviewer trusting the marker's absence has to be
        # able to trust its presence too, so a refused run's header-only file
        # must never carry it.
        fx.corrupt(self.root, "index.latest", b"short")
        path = os.path.join(self.dir, "orphans.tsv")
        code, _out, _err = self.run_cli(
            ["--local-repo", self.root, "--manifest", path])
        self.assertEqual(code, cli.EXIT_REFUSED)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertNotIn(manifest.COMPLETION_MARKER.rstrip("\n"), lines)

    def test_the_marker_never_reaches_stdout(self):
        # Stdout is a pipe another program reads with `cut -f1`, not a file a
        # reviewer reopens later. A `#`-prefixed line there is noise a
        # consumer never asked to filter.
        code, out, _err = self.run_cli(["--local-repo", self.root])
        self.assertEqual(code, 0)
        self.assertNotIn(manifest.COMPLETION_MARKER.rstrip("\n"), out)


class MemoryBudgetFlags(unittest.TestCase):
    """`--max-ram` and `--memory-mb`: units, and never able to disagree."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-cli-budget-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_max_ram_accepts_a_unit_suffixed_size(self):
        args = self.parse(["--local-repo", self.root, "--max-ram", "4GiB"])
        self.assertEqual(args.max_ram, 4 * (1 << 30))

    def test_max_ram_refuses_a_bare_number(self):
        # The defect this flag exists to close: `--memory-mb 4` and
        # `--memory-mb 4096` differ by three orders of magnitude and neither
        # used to be refused. `--max-ram` must never repeat that hole.
        with self.assertRaises(SystemExit):
            self.parse(["--local-repo", self.root, "--max-ram", "4096"])

    def test_max_ram_and_memory_mb_cannot_both_be_set(self):
        # However the two are resolved, they must never be able to name
        # different ceilings for the same run. Refusing to accept both at
        # once is the simplest way to guarantee that.
        with self.assertRaises(SystemExit):
            self.parse(["--local-repo", self.root, "--max-ram", "4GiB",
                       "--memory-mb", "4096"])

    def test_memory_mb_still_works_on_its_own(self):
        # Scripts already passing --memory-mb keep working; this flag was
        # not replaced, only joined.
        args = self.parse(["--local-repo", self.root, "--memory-mb", "512"])
        self.assertEqual(args.memory_mb, 512)
        self.assertIsNone(args.max_ram)

    def test_budget_bytes_prefers_max_ram_over_memory_mb(self):
        # The two cannot both be set per the test above; this pins which one
        # `_budget_bytes` trusts if that mutual exclusion is ever loosened.
        args = self.parse(["--local-repo", self.root, "--max-ram", "4GiB"])
        self.assertEqual(cli._budget_bytes(args), 4 * (1 << 30))

    def test_memory_mb_zero_turns_the_ceiling_off(self):
        args = self.parse(["--local-repo", self.root, "--memory-mb", "0"])
        self.assertIsNone(cli._budget_bytes(args))

    def test_a_shard_directory_too_large_for_max_ram_exits_too_big(self):
        # End to end: a ceiling too small even for this repository's one
        # shard directory has to reach the operator as exit code 5, the same
        # code the old whole-repository gate used.
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--local-repo", self.root, "--max-ram", "1B"],
                        stdin=_Answers(), stdout=out, stderr=err)
        self.assertEqual(code, cli.EXIT_TOO_BIG)
        self.assertIn("--max-ram", err.getvalue())


class FlagSurface(unittest.TestCase):

    def test_every_choice_the_run_makes_can_be_set_without_a_human(self):
        # The prompt refuses rather than blocks when there is no terminal, so
        # the flag surface has to be complete or a scripted caller meets a
        # refusal it cannot answer. A prompt-only option would be unusable
        # from a script and the failure would read as an unexplained refusal.
        flags = {option for action in cli.build_parser()._actions
                 for option in action.option_strings}
        for flag in ("--transport", "--local-repo", "--endpoint", "--region",
                     "--bucket", "--prefix", "--namespace", "--oci-region",
                     "--credentials", "--profile", "--oci-profile",
                     "--elasticsearch", "--es-repository", "--manifest",
                     "--classification", "--coverage-json", "--self-test"):
            self.assertIn(flag, flags)
