"""snapshot_sizes.py: the sizing arithmetic, the exports and the fetch path.

The tool's output feeds capacity decisions and, through --emit-mounted and
--emit-classified, a list of snapshots nothing may delete. These tests drive
the tool the way an operator does, against a loopback Elasticsearch stand-in,
and check what another tool or a person would act on. They do not pin the
wording of messages.
"""

import contextlib
import io
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_sizes as sizes

DAY = 86_400_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % DAY)  # a UTC midnight
GIB = 1024 ** 3


class FakeElasticsearch:
    """A loopback cluster. Routes are matched on the decoded request path.

    `routes` maps a path prefix to (status, body) or a callable returning
    that pair. Every request is recorded with its headers.
    """

    def __init__(self, routes, tls=None):
        self.routes = routes
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                decoded = urllib.parse.unquote(self.path)
                for prefix, answer in owner.routes.items():
                    if decoded.startswith(prefix):
                        status, body = (answer(decoded) if callable(answer)
                                        else answer)
                        break
                else:
                    status, body = 404, {"error": "no route"}
                payload = json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", body["location"])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.scheme = "http"
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket,
                                                    server_side=True)
            self.scheme = "https"
        self.url = f"{self.scheme}://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def status_body(*snaps):
    """_status answer. Each snap is (name, start_ms, incremental, total, state)."""
    return {"snapshots": [
        {"snapshot": n, "state": st,
         "stats": {"start_time_in_millis": ms,
                   "incremental": {"size_in_bytes": inc},
                   "total": {"size_in_bytes": tot}}}
        for n, ms, inc, tot, st in snaps]}


def settings_body(**mounts):
    """_settings answer. mounts: index -> (repo, snapshot, partial, uuid)."""
    out = {}
    for index, (repo, snap, partial, uuid) in mounts.items():
        entry = {"repository_name": repo, "snapshot_name": snap,
                 "partial": partial}
        if uuid:
            entry["snapshot_uuid"] = uuid
        out[index] = {"settings": {"index": {"store": {"snapshot": entry}}}}
    return out


def cluster(snaps, mounts=None, policies=None, repo="r", repo_uuid="U1",
            status_log=None, status_answer=None, listed_uuids=None):
    """Routes for a healthy cluster holding `snaps` in repository `repo`.

    `listed_uuids` maps a snapshot name to the uuid the listing reports for
    it; a name left out is listed without one.

    `status_answer`, when given, turns the names one _status request asked
    for into the body it answers with, in place of the faithful one.
    """
    by_name = {s[0]: s for s in snaps}

    def status(path):
        names = path.split("/")[3].split(",")
        if status_log is not None:
            status_log.append(names)
        if status_answer is not None:
            return 200, status_answer(names)
        return 200, status_body(*[by_name[n] for n in names])

    return {
        f"/_snapshot/{repo}/*?verbose=false": (
            200, {"snapshots": [
                dict({"snapshot": s[0]},
                     **({"uuid": listed_uuids[s[0]]}
                        if s[0] in (listed_uuids or {}) else {}))
                for s in snaps]}),
        f"/_snapshot/{repo}/*?filter_path": (
            200, {"snapshots": [
                {"snapshot": n, "metadata": {"policy": p}}
                for n, p in (policies or {}).items()]}),
        f"/_snapshot/{repo}/": status,
        f"/_snapshot/{repo}": (200, {repo: {"type": "s3", "uuid": repo_uuid}}),
        "/*/_settings": (200, settings_body(**(mounts or {}))),
    }


def limit_file_size(limit):
    """A preexec_fn capping the child's file writes at `limit` bytes."""
    def apply():
        import resource
        import signal
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply


def run_tool(es_url, *argv, env=None):
    """main() in process. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    clean = {k: v for k, v in os.environ.items()
             if k not in ("ES_PASSWORD", "GENCHAIN_ES_API_KEY")}
    clean.update(env or {})
    full = ["snapshot_sizes.py", "--es", es_url, "--repo", "r", *argv]
    with mock.patch.dict(os.environ, clean, clear=True), \
            mock.patch.object(sys, "argv", full), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sizes.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class ServerCase(unittest.TestCase):

    def serve(self, routes, tls=None):
        server = FakeElasticsearch(routes, tls)
        self.addCleanup(server.close)
        return server


# --- sizing arithmetic ------------------------------------------------------

def row(day, name, inc, tot, state="SUCCESS"):
    return (T0 + day * DAY, name, inc, tot, state)


class SizingArithmetic(unittest.TestCase):

    def test_the_total_is_baseline_growth_and_one_baseline_of_headroom(self):
        # An operator buys storage from this number. If the headroom term or
        # the retention multiplier drifted, the repository would be sized too
        # small and fill during the first upgrade day.
        rows = [row(d, f"s{d}", 10, 100 + d) for d in range(7)]
        sizing = sizes.measure_sizing(rows, 7, None)
        self.assertEqual(sizing.baseline, 106)
        self.assertEqual(sizing.total, 106 + 7 * 10 + 106)
        self.assertAlmostEqual(sizing.total_margin, sizing.total * 1.2)

    def test_the_baseline_is_the_largest_total_not_the_newest_row(self):
        # The newest snapshot is often a small per-index mount. Sizing from
        # it would undersize the repository by the whole backup.
        rows = [row(0, "full", 500, 900), row(1, "mount", 5, 40)]
        self.assertEqual(sizes.measure_sizing(rows, 7, None).baseline, 900)

    def test_in_progress_snapshots_never_feed_the_numbers(self):
        # Abuse: a snapshot still running reports partial totals. Counting
        # it as growth would shrink or inflate the recommendation depending
        # on when the report happened to run.
        rows = [row(0, "a", 10, 100), row(1, "b", 99999, 99999, "IN_PROGRESS")]
        sizing = sizes.measure_sizing(rows, 7, None)
        self.assertEqual(sizing.baseline, 100)
        self.assertEqual(sizing.skipped, 1)

    def test_nothing_usable_means_no_recommendation(self):
        # A recommendation built from zero snapshots would read as "0 bytes
        # needed" to whoever is sizing a bucket.
        self.assertIsNone(sizes.measure_sizing([], 7, None))
        self.assertIsNone(sizes.measure_sizing(
            [row(0, "a", 1, 1, "IN_PROGRESS")], 7, None))

    def test_several_snapshots_on_one_day_count_as_one_day_of_growth(self):
        # SLM dailies plus ILM mounts land on the same date. Treating each
        # as its own day would shrink the window and the growth estimate.
        rows = [row(0, "a", 10, 100), row(0, "b", 30, 100),
                row(1, "c", 40, 100)]
        sizing = sizes.measure_sizing(rows, 7, None)
        self.assertEqual(sorted(sizing.samples), [40, 40])

    def test_only_the_last_retention_days_of_data_are_sampled(self):
        # Growth from months ago says nothing about next week. Sampling all
        # of it would size the repository for a workload that ended.
        rows = [row(d, f"s{d}", 1000 if d < 3 else 1, 100) for d in range(12)]
        sizing = sizes.measure_sizing(rows, 5, None)
        self.assertEqual(sizing.samples, [1] * 5)

    def test_p95_is_the_upper_biased_nearest_rank(self):
        # The conservative variant exists so a spiky repository is not sized
        # from its median. A p95 that rounded down would hide the spike.
        self.assertEqual(sizes.p95([1, 2, 3, 4, 100]), 100.0)
        self.assertEqual(sizes.p95([7]), 7.0)

    def test_split_sizing_uses_only_slm_snapshots_and_adds_the_frozen_total(self):
        # A pinned mount snapshot is a footprint floor, not growth. If it
        # fed the baseline the frozen tier would be counted twice.
        rows = [row(0, "daily", 10, 100), row(1, "mount", 5000, 5000)]
        split = {"labels": {"daily": sizes.CLASS_SLM,
                            "mount": sizes.CLASS_FROZEN},
                 "mounted": {"mount": {"partial": True}}, "policies": {}}
        sizing = sizes.measure_sizing(rows, 7, split)
        self.assertEqual(sizing.baseline, 100)
        self.assertEqual(sizing.frozen_total, 5000)
        self.assertEqual(sizing.total, 100 + 7 * 10 + 100 + 5000)
        self.assertEqual(sizing.excluded, 1)

    def test_split_sizing_with_no_slm_snapshot_recommends_nothing(self):
        # Abuse: a repository holding only mounts. A baseline taken from
        # them would be a number with no backup behind it.
        rows = [row(0, "mount", 5, 50)]
        split = {"labels": {"mount": sizes.CLASS_FROZEN}, "mounted": {},
                 "policies": {}}
        self.assertIsNone(sizes.measure_sizing(rows, 7, split))

    def test_period_keys_group_by_utc_calendar(self):
        # A report grouped by local time would put the same snapshot in
        # different periods on different hosts.
        ms = 1_704_067_199_000  # 2023-12-31T23:59:59Z, an ISO week 52 Sunday
        self.assertEqual(sizes.period_key(ms, "day"), "2023-12-31")
        self.assertEqual(sizes.period_key(ms, "week"), "2023-W52")
        self.assertEqual(sizes.period_key(ms, "month"), "2023-12")

    def test_fmt_scales_to_the_unit_an_operator_can_read(self):
        # A byte count printed raw, or in the wrong unit, is how a 10x
        # sizing mistake gets made.
        self.assertEqual(sizes.fmt(512), "512.0 B")
        self.assertEqual(sizes.fmt(3 * GIB), "3.0 GiB")
        self.assertEqual(sizes.fmt(5 * 1024 ** 5), "5,120.0 TiB")


class Classification(unittest.TestCase):

    def test_a_policy_snapshot_that_is_also_mounted_buckets_as_frozen(self):
        # Treating a mounted policy snapshot as a backup would count it as
        # growth and imply retention may reap it, which destroys the index.
        label = sizes.classify_snapshot("a", {"a": "daily"}, {"a": {}})
        self.assertEqual(label, sizes.CLASS_BOTH)
        self.assertEqual(sizes.class_bucket(label), sizes.CLASS_FROZEN)

    def test_every_other_snapshot_gets_exactly_one_class(self):
        # The export's class column is what a delete guard reads.
        policies, mounted = {"p": "daily"}, {"m": {}}
        self.assertEqual(sizes.classify_snapshot("p", policies, mounted),
                         sizes.CLASS_SLM)
        self.assertEqual(sizes.classify_snapshot("m", policies, mounted),
                         sizes.CLASS_FROZEN)
        self.assertEqual(sizes.classify_snapshot("x", policies, mounted),
                         sizes.CLASS_OTHER)

    def test_totals_split_partial_from_full_mounts(self):
        # The frozen footprint drives the second half of the sizing. A
        # snapshot backing a partial mount must land in the frozen tier.
        rows = [(0, "p", 1, 10, "SUCCESS"), (0, "f", 1, 20, "SUCCESS"),
                (0, "s", 4, 40, "SUCCESS")]
        split = {"labels": {"p": sizes.CLASS_BOTH, "f": sizes.CLASS_FROZEN,
                            "s": sizes.CLASS_SLM},
                 "mounted": {"p": {"partial": True}, "f": {"partial": False}}}
        agg, frozen = sizes.split_totals(rows, split)
        self.assertEqual(frozen["total"], 30)
        self.assertEqual((frozen["partial_n"], frozen["partial_tot"]), (1, 10))
        self.assertEqual((frozen["full_n"], frozen["full_tot"]), (1, 20))
        self.assertEqual(frozen["both_n"], 1)
        self.assertEqual(agg[sizes.CLASS_SLM]["inc"], 4)

    def test_an_unlabelled_snapshot_is_counted_as_other_not_dropped(self):
        # Abuse: a snapshot that appears between the listing and the status
        # call has no label. Dropping it would make the totals quietly low.
        agg, _ = sizes.split_totals([(0, "new", 1, 2, "SUCCESS")],
                                    {"labels": {}})
        self.assertEqual(agg[sizes.CLASS_OTHER]["n"], 1)

    def test_a_mount_with_no_uuid_and_a_second_mounting_index(self):
        # A snapshot mounted by two indices has to name both, and the later
        # index must not erase a uuid the first one carried.
        data = {"i1": settings_body(i1=("r", "s", "false", "UU"))["i1"],
                "i2": settings_body(i2=("r", "s", "true", None))["i2"],
                "other": settings_body(other=("elsewhere", "t", "true",
                                              None))["other"],
                "plain": {"settings": {"index": {"number_of_shards": 1}}}}
        with mock.patch.object(sizes, "http_get", return_value=data):
            mounted = sizes.fetch_mounted_set(mock.Mock(repo="r"))
        self.assertEqual(list(mounted), ["s"])
        self.assertEqual(sorted(mounted["s"]["indices"]), ["i1", "i2"])
        self.assertTrue(mounted["s"]["partial"] and mounted["s"]["full"])
        self.assertEqual(mounted["s"]["uuid"], "UU")


class ClassFilter(unittest.TestCase):

    def test_no_filter_means_every_class(self):
        # --class is optional. Treating absence as "nothing" would write an
        # empty export that looks like a clean repository.
        self.assertIsNone(sizes.parse_class_filter(None))

    def test_a_filter_keeps_order_and_drops_repeats(self):
        self.assertEqual(sizes.parse_class_filter("slm, other,slm"),
                         ["slm", "other"])

    def test_an_unknown_class_is_refused(self):
        # Abuse: a typo such as "frozen" would otherwise filter to nothing
        # and the export would read as a repository with no pinned snapshots.
        with self.assertRaises(ValueError):
            sizes.parse_class_filter("frozen")

    def test_the_mounted_label_is_not_selectable(self):
        # slm+mounted is a label, not a bucket. Accepting it would promise
        # a split the rest of the tool does not make.
        with self.assertRaises(ValueError):
            sizes.parse_class_filter(sizes.CLASS_BOTH)

    def test_an_empty_filter_is_refused(self):
        with self.assertRaises(ValueError):
            sizes.parse_class_filter(" , ")

    def test_the_filter_selects_on_bucket_so_frozen_catches_mounted_policy(self):
        # --class frozen-pinned feeds a delete guard. If it missed the
        # slm+mounted snapshots, the guard would let one be deleted.
        rows = [("a", sizes.CLASS_BOTH), ("b", sizes.CLASS_SLM)]
        kept = sizes.filter_classified(rows, ["frozen-pinned"])
        self.assertEqual([r[0] for r in kept], ["a"])
        self.assertEqual(len(sizes.filter_classified(rows, None)), 2)

    def test_the_classified_export_keeps_snapshots_missing_from_the_catalog(self):
        # A mount pinning a deleted snapshot is the riskiest state there is.
        # An export that left it out would say the repository is clean.
        rows = [(T0 + DAY, "b", 5, 50, "SUCCESS"), (T0, "a", 1, 10, "SUCCESS")]
        split = {"labels": {"a": sizes.CLASS_SLM, "b": sizes.CLASS_SLM},
                 "mounted": {"gone": {"partial": True, "indices": ["ix"]}},
                 "policies": {"a": "daily"}}
        out = sizes.classified_rows(rows, split, ["gone"])
        self.assertEqual([r[0] for r in out], ["gone", "a", "b"])
        gone = out[0]
        self.assertEqual(gone[1], sizes.CLASS_FROZEN)
        self.assertEqual(gone[5], sizes.MISSING_STATE)
        self.assertEqual(gone[3:5], ("partial", "ix"))
        self.assertEqual(out[1][2], "daily")

    def test_a_missing_start_stamp_is_a_dash_not_1970(self):
        # A 1970 date in the export would sort a snapshot as the oldest and
        # let an age-based rule treat it as expired.
        self.assertEqual(sizes.iso_utc(0), "-")
        self.assertEqual(sizes.iso_utc(T0), "2023-11-14T00:00:00Z")


# --- fetch path and the exports through main() ------------------------------

SNAPS = [
    ("daily-1", T0, 10 * GIB, 10 * GIB, "SUCCESS"),
    ("daily-2", T0 + DAY, 1 * GIB, 11 * GIB, "SUCCESS"),
    ("mount-1", T0 + DAY, 0, 3 * GIB, "SUCCESS"),
]
MOUNTS = {"restored-ix": ("r", "mount-1", "true", "MU1")}
POLICIES = {"daily-1": "nightly", "daily-2": "nightly"}


class EmitMounted(ServerCase):

    def test_the_export_lists_each_pinned_snapshot_under_a_provenance_line(self):
        # Another tool reads the first token of each line to learn what it
        # must not delete. A changed layout reads as an empty set, which is
        # what a passed gate looks like.
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "# repository: r U1", "mount-1\tMU1\tpartial\trestored-ix"])

    def test_a_hidden_mounted_index_is_in_the_pinned_set(self):
        # Elasticsearch leaves hidden indices out of a wildcard unless asked.
        # A mount of a hidden index, such as one under a hidden alias, then
        # pins a snapshot the export never names, and whatever reads the
        # export deletes it under a live index. The stand-in answers the way
        # Elasticsearch does.
        hidden = settings_body(**{".hidden-mount": ("r", "mount-1", "true",
                                                    "MU1")})

        def answer(path):
            return 200, hidden if "expand_wildcards=all" in path else {}

        routes = cluster(SNAPS)
        routes["/*/_settings"] = answer
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual(code, 0)
        self.assertIn("mount-1", [line.split("\t")[0]
                                  for line in out.splitlines()])

    def test_a_snapshot_with_no_uuid_gets_a_dash_placeholder(self):
        # A blank field would shift the columns the consumer parses.
        mounts = {"ix": ("r", "mount-1", "false", None)}
        es = self.serve(cluster(SNAPS, mounts))
        _, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual(out.splitlines()[1], "mount-1\t-\tfull\tix")

    def test_a_repository_name_that_does_not_exist_writes_nothing_and_fails(self):
        # Abuse: one mistyped character in --repo. An empty list printed
        # with exit 0 disarms the mounted-snapshot check.
        routes = cluster(SNAPS, MOUNTS)
        routes["/_snapshot/r"] = (200, {})
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual((code, out), (1, ""))

    def test_an_unresolvable_repository_writes_nothing_and_fails(self):
        routes = cluster(SNAPS, MOUNTS)
        routes["/_snapshot/r"] = (404, {"error": "missing"})
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual((code, out), (1, ""))

    def test_a_repository_answer_that_is_not_an_object_is_refused(self):
        # Abuse: a proxy answers 200 with a JSON list. Treating it as a
        # registered repository would print an empty, passing list.
        routes = cluster(SNAPS, MOUNTS)
        routes["/_snapshot/r"] = (200, [])
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual((code, out), (1, ""))

    def test_a_failed_mount_discovery_writes_nothing_and_fails(self):
        # A partial export would miss pinned snapshots and let them be
        # deleted.
        routes = cluster(SNAPS, MOUNTS)
        routes["/*/_settings"] = (500, {"error": "boom"})
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-mounted")
        self.assertEqual((code, out), (1, ""))

    def test_out_writes_the_same_lines_to_a_file(self):
        # Pipelines pass --out so a failed run cannot leave a truncated
        # file from a shell redirect.
        es = self.serve(cluster(SNAPS, MOUNTS))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mounted.tsv")
            code, out, _ = run_tool(es.url, "--emit-mounted", "--out", path)
            with open(path) as handle:
                written = handle.read().splitlines()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(written[1], "mount-1\tMU1\tpartial\trestored-ix")

    def test_an_unwritable_out_path_fails_before_any_request(self):
        # Abuse: a directory that does not exist. Exiting 0 would hand the
        # next stage a file that is not there, and finding out after every
        # fetch wastes a run against a production cluster.
        es = self.serve(cluster(SNAPS, MOUNTS))
        bad = os.path.join(tempfile.gettempdir(), "no-such-dir-106", "x.tsv")
        code, _, _ = run_tool(es.url, "--emit-mounted", "--out", bad)
        self.assertNotEqual(code, 0)
        self.assertEqual(es.requests, [])


class OutIsWrittenWholeOrNotAtAll(ServerCase):
    """--out holds the set of snapshots nothing may delete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mounted.tsv")
        with open(self.path, "w") as handle:
            handle.write("# repository: r U1\nkeep-me\t-\tfull\tix\n")
        self.old = open(self.path).read()

    def many_mounts(self):
        mounts = {f"ix-{i}": ("r", f"mount-{i:03d}", "true", f"U{i}")
                  for i in range(60)}
        return self.serve(cluster(SNAPS, mounts))

    def test_a_write_cut_off_part_way_leaves_the_earlier_file_whole(self):
        # A full disk, a quota or a killed process cut the write short. A
        # truncated file reads as a complete, shorter pinned set, and every
        # snapshot past the cut becomes deletable. The child runs under a
        # file size limit smaller than the export.
        es = self.many_mounts()
        done = subprocess.run(
            [sys.executable, "-B", os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "snapshot_sizes.py"),
             "--es", es.url, "--repo", "r", "--emit-mounted",
             "--out", self.path],
            capture_output=True, text=True, timeout=60,
            preexec_fn=limit_file_size(400))
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(open(self.path).read(), self.old)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["mounted.tsv"])

    def test_a_read_only_out_file_is_refused_and_left_alone(self):
        # An operator write-protects a pinned set to keep it. Replacing it
        # with exit 0 because the directory is writable would overwrite the
        # copy they meant to keep.
        if os.geteuid() == 0:
            self.skipTest("root writes a 0444 file regardless")
        os.chmod(self.path, 0o444)
        es = self.serve(cluster(SNAPS, MOUNTS))
        code, _, _ = run_tool(es.url, "--emit-mounted", "--out", self.path)
        self.assertNotEqual(code, 0)
        self.assertEqual(open(self.path).read(), self.old)
        self.assertEqual(es.requests, [])

    def test_a_replaced_out_file_keeps_its_mode(self):
        # Whoever set the mode on the pinned set chose who may read it. A
        # rewrite that reset it would widen or narrow that silently.
        os.chmod(self.path, 0o640)
        es = self.serve(cluster(SNAPS, MOUNTS))
        code, _, _ = run_tool(es.url, "--emit-mounted", "--out", self.path)
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertIn("mount-1", open(self.path).read())

    def test_a_device_out_is_written_directly(self):
        # /dev/null and a pipe cannot be replaced by a rename. They must
        # still receive the export, or a process-substitution consumer
        # reads an empty set.
        es = self.serve(cluster(SNAPS, MOUNTS))
        code, _, _ = run_tool(es.url, "--emit-mounted", "--out", os.devnull)
        self.assertEqual(code, 0)


class EmitClassified(ServerCase):

    def rows(self, out):
        return [line.split("\t") for line in out.splitlines()]

    def test_each_snapshot_is_one_row_under_the_documented_header(self):
        # Retention tooling selects on these columns by position.
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        code, out, _ = run_tool(es.url, "--emit-classified")
        table = self.rows(out)
        self.assertEqual(code, 0)
        self.assertEqual(tuple(table[0]), sizes.CLASSIFIED_HEADER)
        by_name = {r[0]: r for r in table[1:]}
        self.assertEqual(by_name["daily-1"][1:4], ["slm", "nightly", "-"])
        self.assertEqual(by_name["mount-1"][1:5],
                         ["frozen-pinned", "-", "partial", "restored-ix"])
        self.assertEqual(by_name["daily-2"][7:], [str(1 * GIB), str(11 * GIB)])

    def test_a_class_filter_changes_the_file_but_not_the_danger_banner(self):
        # A snapshot deleted while mounted must be announced even when the
        # operator asked only for slm rows.
        mounts = dict(MOUNTS, ghost=("r", "vanished", "true", None))
        es = self.serve(cluster(SNAPS, mounts, POLICIES))
        code, out, err = run_tool(es.url, "--emit-classified",
                                  "--class", "slm")
        self.assertEqual(code, 0)
        self.assertEqual({r[0] for r in self.rows(out)[1:]},
                         {"daily-1", "daily-2"})
        self.assertIn("vanished", err)

    def test_a_snapshot_missing_from_the_listing_is_exported_as_missing(self):
        mounts = dict(MOUNTS, ghost=("r", "vanished", "true", None))
        es = self.serve(cluster(SNAPS, mounts, POLICIES))
        _, out, _ = run_tool(es.url, "--emit-classified")
        gone = [r for r in self.rows(out) if r[0] == "vanished"]
        self.assertEqual(gone[0][1], "frozen-pinned")
        self.assertEqual(gone[0][5], sizes.MISSING_STATE)

    def test_a_failed_mount_discovery_writes_no_file_at_all(self):
        # Abuse: without the mount linkage every pinned snapshot would be
        # exported as a plain backup. Failing loudly is the only safe answer.
        routes = cluster(SNAPS, MOUNTS, POLICIES)
        routes["/*/_settings"] = (500, {"error": "boom"})
        es = self.serve(routes)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.tsv")
            code, out, _ = run_tool(es.url, "--emit-classified",
                                    "--out", path)
            exists = os.path.exists(path)
        self.assertEqual((code, out, exists), (1, "", False))

    def test_a_failed_policy_fetch_writes_nothing(self):
        routes = cluster(SNAPS, MOUNTS, POLICIES)
        routes["/_snapshot/r/*?filter_path"] = (500, {"error": "boom"})
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-classified")
        self.assertEqual((code, out), (1, ""))

    def test_a_failed_status_batch_writes_nothing(self):
        # A classified export built from some snapshots would be read as the
        # whole repository.
        routes = cluster(SNAPS, MOUNTS, POLICIES)
        routes["/_snapshot/r/"] = (500, {"error": "boom"})
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--emit-classified")
        self.assertEqual((code, out), (1, ""))

    def test_an_unusable_status_answer_writes_nothing(self):
        # The export is the list retention tooling reads. A pinned snapshot
        # that _status left out disappears from it, and one with no stats is
        # exported with sizes of 0, and either way the run exited 0.
        by_name = {s[0]: s for s in SNAPS}

        def faithful(names):
            return status_body(*[by_name[n] for n in names])

        def omitting_mount(names):
            return status_body(*[by_name[n] for n in names
                                 if n != "mount-1"])

        def null_stats(names):
            body = faithful(names)
            body["snapshots"][0]["stats"] = None
            return body

        def nameless(names):
            body = faithful(names)
            del body["snapshots"][0]["snapshot"]
            return body

        def sizeless(names):
            body = faithful(names)
            del body["snapshots"][0]["stats"]["total"]
            return body

        for answer in (omitting_mount, null_stats, nameless, sizeless):
            with self.subTest(answer=answer.__name__):
                es = self.serve(cluster(SNAPS, MOUNTS, POLICIES,
                                        status_answer=answer))
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "c.tsv")
                    code, out, err = run_tool(es.url, "--emit-classified",
                                              "--out", path)
                    exists = os.path.exists(path)
                self.assertEqual((code, out, exists), (1, "", False))
                self.assertNotIn("Traceback", err)

    def test_a_mount_of_an_older_snapshot_with_a_reused_name_is_missing(self):
        # A snapshot deleted and re-created under the same name has a new
        # uuid. The mount still reads the old one's blobs, which no listed
        # snapshot references, and a name-only check calls it safe.
        mounts = {"restored-ix": ("r", "mount-1", "true", "OLD")}
        es = self.serve(cluster(SNAPS, mounts, POLICIES,
                                listed_uuids={"mount-1": "NEW"}))
        _, out, _ = run_tool(es.url, "--emit-classified")
        states = [r[5] for r in self.rows(out) if r[0] == "mount-1"]
        self.assertIn(sizes.MISSING_STATE, states)

    def test_a_mount_of_the_listed_snapshot_is_not_missing(self):
        # The counterpart: the same uuid on both sides is the healthy case,
        # and a false MISSING row would send the operator to remount a
        # working index.
        mounts = {"restored-ix": ("r", "mount-1", "true", "SAME")}
        es = self.serve(cluster(SNAPS, mounts, POLICIES,
                                listed_uuids={"mount-1": "SAME"}))
        _, out, _ = run_tool(es.url, "--emit-classified")
        states = [r[5] for r in self.rows(out) if r[0] == "mount-1"]
        self.assertNotIn(sizes.MISSING_STATE, states)

    def test_an_empty_repository_is_an_error_not_an_empty_export(self):
        # An empty file is what "nothing to protect" looks like downstream.
        es = self.serve(cluster([], None, None))
        code, out, _ = run_tool(es.url, "--emit-classified")
        self.assertEqual((code, out), (1, ""))

    def test_an_unwritable_out_path_fails(self):
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        bad = os.path.join(tempfile.gettempdir(), "no-such-dir-106", "x.tsv")
        code, _, _ = run_tool(es.url, "--emit-classified", "--out", bad)
        self.assertNotEqual(code, 0)
        self.assertEqual(es.requests, [])

    def test_out_receives_the_table_and_stdout_stays_empty(self):
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.tsv")
            code, out, _ = run_tool(es.url, "--emit-classified",
                                    "--out", path)
            with open(path) as handle:
                lines = handle.read().splitlines()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(lines), 1 + len(SNAPS))


class PeriodReport(ServerCase):

    def test_a_batch_smaller_than_the_listing_fetches_every_snapshot(self):
        # Operators lower --batch when a cluster times out. Dropping a
        # remainder chunk would silently under-report the repository.
        snaps = [(f"s{i}", T0 + i * DAY, 1, 10, "SUCCESS") for i in range(5)]
        log = []
        es = self.serve(cluster(snaps, status_log=log))
        code, out, _ = run_tool(es.url, "--batch", "2")
        self.assertEqual(code, 0)
        self.assertEqual([len(b) for b in log], [2, 2, 1])
        self.assertIn("SUM", out)
        self.assertEqual(sum(1 for line in out.splitlines()
                             if line.startswith("2023-")), 5)

    def test_snapshot_names_are_encoded_into_one_path_segment(self):
        # Snapshot names come back from the cluster. A slash or question
        # mark in one must not aim the request at another API.
        snaps = [("a/b?c", T0, 1, 10, "SUCCESS")]
        log = []
        es = self.serve(cluster(snaps, status_log=log))
        run_tool(es.url)
        status_paths = [p for p, _ in es.requests if p.endswith("/_status")]
        self.assertEqual(len(status_paths), 1)
        self.assertEqual(status_paths[0].count("/"), 4)
        self.assertNotIn("?", status_paths[0])

    def test_a_failing_batch_discards_the_report(self):
        # A table built from the batches that worked would be read as the
        # whole repository.
        snaps = [(f"s{i}", T0, 1, 10, "SUCCESS") for i in range(4)]
        routes = cluster(snaps)
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) == 2:
                return 500, {"error": "boom"}
            names = path.split("/")[3].split(",")
            return 200, status_body(*[s for s in snaps if s[0] in names])
        routes["/_snapshot/r/"] = flaky
        es = self.serve(routes)
        code, out, _ = run_tool(es.url, "--batch", "2")
        self.assertEqual((code, out), (1, ""))

    def test_http_errors_and_unreachable_clusters_exit_1(self):
        # Credentials wrong or port-forward down: the exit code is what a
        # cron wrapper alerts on.
        routes = cluster(SNAPS)
        routes["/_snapshot/r/*?verbose=false"] = (401, {"error": "no"})
        es = self.serve(routes)
        self.assertEqual(run_tool(es.url)[0], 1)
        self.assertEqual(run_tool("http://127.0.0.1:1")[0], 1)

    def test_an_empty_repository_exits_1(self):
        # Reporting success on zero snapshots hides a wrong --repo.
        es = self.serve(cluster([]))
        self.assertEqual(run_tool(es.url)[0], 1)

    def test_split_frozen_reports_classes_and_warns_about_a_missing_mount(self):
        # The operator needs the per-class footprint and the loud banner for
        # a mount whose snapshot was deleted.
        mounts = dict(MOUNTS, ghost=("r", "vanished", "true", None))
        es = self.serve(cluster(SNAPS, mounts, POLICIES))
        code, out, err = run_tool(es.url, "--split-frozen", "--recommend")
        self.assertEqual(code, 0)
        self.assertIn("vanished", err)
        for cls in sizes.CLASS_ORDER:
            self.assertIn(cls, out)
        self.assertIn(sizes.fmt(3 * GIB), out)

    def test_split_frozen_falls_back_to_the_plain_report_when_discovery_fails(self):
        # The plain report is still useful, but it must not claim to have
        # split anything, and the recommendation must stay unsplit.
        routes = cluster(SNAPS, MOUNTS, POLICIES)
        routes["/*/_settings"] = (500, {"error": "boom"})
        es = self.serve(routes)
        code, out, err = run_tool(es.url, "--split-frozen", "--recommend")
        self.assertEqual(code, 0)
        self.assertNotIn(sizes.CLASS_FROZEN, out.split("NOTE:")[0])
        self.assertIn("skipped", err)

    def test_the_policy_fetch_failing_also_falls_back(self):
        routes = cluster(SNAPS, MOUNTS, POLICIES)
        routes["/_snapshot/r/*?filter_path"] = (500, {"error": "boom"})
        es = self.serve(routes)
        self.assertEqual(run_tool(es.url, "--split-frozen")[0], 0)

    def test_recommend_prints_the_computed_capacity(self):
        # The figure in the report is what gets bought. It must be the one
        # measure_sizing computed, in readable units.
        snaps = [(f"s{d}", T0 + d * DAY, 1 * GIB, 10 * GIB, "SUCCESS")
                 for d in range(7)]
        es = self.serve(cluster(snaps))
        code, out, _ = run_tool(es.url, "--recommend")
        want = sizes.measure_sizing(
            [(s[1], s[0], s[2], s[3], s[4]) for s in snaps], 7, None)
        self.assertEqual(code, 0)
        self.assertIn(sizes.fmt(want.total), out)
        self.assertIn(sizes.fmt(want.total_margin), out)

    def test_recommend_with_only_in_progress_snapshots_gives_no_figure(self):
        snaps = [("s", T0, 1, 1, "IN_PROGRESS")]
        es = self.serve(cluster(snaps))
        code, out, _ = run_tool(es.url, "--recommend")
        self.assertEqual(code, 0)
        self.assertNotIn("recommended repository capacity", out)

    def test_recommend_flags_a_window_that_includes_the_first_snapshot(self):
        # The first snapshot is a full upload. Without the caveat the growth
        # figure overstates and the repository is oversized.
        snaps = [("a", T0, 50 * GIB, 50 * GIB, "SUCCESS"),
                 ("b", T0 + DAY, 1, 50 * GIB, "SUCCESS")]
        es = self.serve(cluster(snaps))
        _, out, _ = run_tool(es.url, "--recommend")
        self.assertIn("FIRST snapshot day", out)

    def test_recommend_flags_an_outlier_day_and_partial_snapshots(self):
        snaps = [(f"s{d}", T0 + d * DAY, 1 * GIB, 20 * GIB, "SUCCESS")
                 for d in range(1, 6)]
        snaps.append(("big", T0 + 6 * DAY, 100 * GIB, 100 * GIB, "PARTIAL"))
        snaps.append(("run", T0 + 7 * DAY, 1, 1, "IN_PROGRESS"))
        es = self.serve(cluster(snaps))
        _, out, _ = run_tool(es.url, "--recommend")
        self.assertIn("outlier day", out)
        self.assertIn("PARTIAL snapshot(s) included", out)
        self.assertIn("1 snapshot(s) excluded", out)

    def test_recommend_split_mentions_the_excluded_mount_snapshots(self):
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        _, out, _ = run_tool(es.url, "--split-frozen", "--recommend")
        self.assertIn("1 non-slm snapshot(s) excluded", out)


# --- credentials on the wire ------------------------------------------------

class CredentialsOnTheWire(ServerCase):

    def secret(self, text):
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(text)
        handle.close()
        os.chmod(handle.name, 0o600)
        self.addCleanup(os.unlink, handle.name)
        # Secret files must sit under GENCHAIN_SECRET_ROOT or the working
        # directory; this one is in the system temp directory.
        root = mock.patch.dict(
            os.environ, {"GENCHAIN_SECRET_ROOT": os.path.dirname(handle.name)})
        root.start()
        self.addCleanup(root.stop)
        return handle.name

    def test_a_basic_auth_run_sends_the_header_to_every_request(self):
        # A missed request would be a 401 on the one call that mattered,
        # reported as a cluster fault.
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES))
        path = self.secret("pw\n")
        code, _, _ = run_tool(es.url, "--user", "bob",
                              "--password-file", path, "--emit-classified")
        self.assertEqual(code, 0)
        heads = {h.get("Authorization") for _, h in es.requests}
        self.assertEqual(heads, {"Basic Ym9iOnB3"})

    def test_a_directory_is_refused_as_a_secret_file(self):
        # A path typo that lands on a directory must fail at the command
        # line, not as a 401 later.
        es = self.serve(cluster(SNAPS))
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = run_tool(es.url, "--user", "bob",
                                  "--password-file", tmp)
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_an_api_key_file_and_the_environment_together_are_refused(self):
        # Two keys make the identity of a run a guess.
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--api-key-file", self.secret("a:b"),
                              env={"GENCHAIN_ES_API_KEY": "c:d"})
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_a_password_without_a_user_is_refused(self):
        # A password set in the environment of a shared runner would
        # otherwise be silently ignored, leaving the run unauthenticated.
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, env={"ES_PASSWORD": "pw"})
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])


class Redirects(ServerCase):

    def test_a_redirect_fails_the_run_and_the_target_is_never_contacted(self):
        # Following it would hand the credential to whatever host the
        # redirect names.
        target = self.serve({"/": (200, {})})
        es = self.serve({"/": (302, {"location": target.url + "/x"})})
        code, _, _ = run_tool(es.url)
        self.assertEqual(code, 1)
        self.assertEqual(target.requests, [])

    def test_the_refusal_names_the_host_but_not_the_query_string(self):
        # A redirect Location can carry a token. The error text ends up in
        # logs.
        err = sizes.RedirectRefused(
            302, "https://evil.example:8443/p?token=SECRET#frag")
        self.assertEqual(err.code, 302)
        self.assertIn("evil.example:8443", str(err))
        self.assertNotIn("SECRET", str(err))

    def test_an_ipv6_location_keeps_its_brackets(self):
        err = sizes.RedirectRefused(301, "http://[::1]:9200/x")
        self.assertIn("[::1]:9200", str(err))

    def test_an_unparseable_location_still_produces_a_refusal(self):
        # Abuse: a malformed Location header must not turn the refusal into
        # a ValueError that escapes the request-failure handlers.
        err = sizes.RedirectRefused(302, "http://[bad/x")
        self.assertIsInstance(err, urllib.error.URLError)


# --- TLS --------------------------------------------------------------------

class Tls(ServerCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cert = os.path.join(cls.tmp.name, "cert.pem")
        cls.key = os.path.join(cls.tmp.name, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", cls.key, "-out", cls.cert, "-days", "2",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_cluster_with_its_own_ca_is_reached_by_naming_that_ca(self):
        # This is the only way a lab cluster is reached, since there is no
        # switch to turn verification off.
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES),
                        tls=(self.cert, self.key))
        code, out, _ = run_tool(es.url, "--ca-cert", self.cert)
        self.assertEqual(code, 0)
        self.assertIn("SUM", out)

    def test_an_untrusted_certificate_is_refused(self):
        # Abuse: without --ca-cert the self-signed certificate must fail.
        # If it connected, credentials would go to any host that can answer.
        es = self.serve(cluster(SNAPS, MOUNTS, POLICIES),
                        tls=(self.cert, self.key))
        code, out, _ = run_tool(es.url)
        self.assertEqual((code, out), (1, ""))

    def test_a_ca_file_that_is_not_a_certificate_is_refused_at_the_start(self):
        # A wrong path must fail at the command line, not read as a broken
        # cluster after the first connection.
        junk = os.path.join(self.tmp.name, "junk.pem")
        with open(junk, "w") as handle:
            handle.write("not a certificate")
        es = self.serve(cluster(SNAPS))
        for bad in (junk, os.path.join(self.tmp.name, "absent.pem")):
            code, _, _ = run_tool(es.url, "--ca-cert", bad)
            self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_a_ca_cert_for_a_plain_http_endpoint_is_refused(self):
        # A CA named for an http cluster is never used, so the operator who
        # passed it believes the connection is verified when nothing is.
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--ca-cert", self.cert)
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_the_context_is_verified_and_floors_tls_at_1_2(self):
        # The floor must not depend on the host's OpenSSL build.
        args = mock.Mock(es="https://es.example:9200", ca_cert=self.cert)
        context = sizes.tls_context(args)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_plain_http_endpoint_gets_no_tls_context(self):
        args = mock.Mock(es="http://es.example:9200", ca_cert=None)
        self.assertIsNone(sizes.tls_context(args))


# --- command line -----------------------------------------------------------

class CommandLine(ServerCase):

    def test_insecure_is_refused_and_points_at_ca_cert(self):
        # Saved command lines still carry --insecure. They must stop, and
        # the operator needs the working alternative, not a bare error.
        es = self.serve(cluster(SNAPS))
        code, _, err = run_tool(es.url, "--insecure")
        self.assertEqual(code, 2)
        self.assertIn(sizes.CA_CERT_EXTRACTION, err)
        self.assertEqual(es.requests, [])

    def test_unrelated_parser_errors_do_not_carry_the_insecure_advice(self):
        es = self.serve(cluster(SNAPS))
        code, _, err = run_tool(es.url, "--no-such-flag")
        self.assertEqual(code, 2)
        self.assertNotIn(sizes.CA_CERT_EXTRACTION, err)

    def test_the_retention_window_is_bounded_by_site_policy(self):
        # The recommendation is only valid for a 5-10 day window.
        es = self.serve(cluster(SNAPS))
        for days, want in (("4", 2), ("11", 2), ("5", 0), ("10", 0)):
            self.assertEqual(run_tool(es.url, "--retention-days", days)[0],
                             want, days)

    def test_the_two_emit_modes_cannot_be_combined(self):
        # Two exports on one stdout would interleave into an unparseable file.
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--emit-mounted", "--emit-classified")
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_out_without_an_emit_mode_is_refused(self):
        # The human tables must never be redirected into a file a tool might
        # parse.
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--out", "x")
        self.assertEqual(code, 2)

    def test_class_without_emit_classified_is_refused(self):
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--class", "slm")
        self.assertEqual(code, 2)

    def test_an_unknown_class_is_refused_before_any_request(self):
        es = self.serve(cluster(SNAPS))
        code, _, _ = run_tool(es.url, "--emit-classified", "--class", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(es.requests, [])

    def test_the_endpoint_is_rebuilt_without_a_query_or_fragment(self):
        # --es is configuration, not trust. A query typed into it must not
        # reappear inside a request path.
        es = self.serve(cluster(SNAPS))
        run_tool(es.url + "/?x=1#y", "--emit-mounted")
        self.assertTrue(es.requests)
        for path, _ in es.requests:
            self.assertNotIn("x=1", path)

    def test_a_non_http_endpoint_is_refused(self):
        # file:// and similar schemes would let configuration read local
        # files through urllib.
        for bad in ("file:///etc/passwd", "http://"):
            code, _, _ = run_tool(bad)
            self.assertEqual(code, 2, bad)


if __name__ == "__main__":
    unittest.main()
