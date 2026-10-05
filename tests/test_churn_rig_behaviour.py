"""The churn rig's setup, report, run and teardown decisions, with no cluster.

Each case drives the rig's own functions against an in-memory stand-in for
Elasticsearch and for the object store. What is pinned here is what an
operator acts on: what a report says is leaking, what teardown refuses to
touch, and what it leaves behind when it cannot finish.
"""

import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import snapshot_churn_rig as rig


class FakeEs:
    """Answers the calls the rig makes from dictionaries and records writes."""

    def __init__(self, version="8.15.0"):
        self.version = version
        self.present = {}
        self.nodes = {"n1": {"roles": ["data_frozen"], "settings": {
            "xpack": {"searchable": {"snapshot": {
                "shared_cache": {"size": "1gb"}}}}}}}
        self.persistent = {}
        self.calls = []
        self.put_failures = {}
        self.bulk_response = {"errors": False, "items": []}
        self.bulk_error = None

    def get(self, path, ok=(200,), timeout=60):
        if path == "/":
            return {"version": {"number": self.version}}
        if path.startswith("/_nodes"):
            return {"nodes": self.nodes}
        if path.startswith("/_cluster/settings"):
            return {"persistent": dict(self.persistent)}
        raise AssertionError("unexpected GET " + path)

    def get_or_none(self, path, timeout=60):
        return self.present.get(path)

    def put(self, path, body=None, timeout=120):
        failure = self.put_failures.pop(path, None)
        if failure is not None:
            raise failure
        self.calls.append(("PUT", path, body))
        if path == "/_cluster/settings":
            self.persistent.update(body["persistent"])

    def delete(self, path, ok=(200, 404), timeout=300):
        self.calls.append(("DELETE", path, None))

    def req(self, method, path, body=None, ok=(200, 201), timeout=60,
            ndjson=None):
        if self.bulk_error:
            raise self.bulk_error
        self.calls.append((method, path, ndjson))
        return 200, self.bulk_response

    def paths(self, method):
        return [p for m, p, _ in self.calls if m == method]


class FakeS3:
    bucket = "bucket"

    def __init__(self, objects=()):
        self.objects = dict(objects)
        self.deleted = []

    def list(self, prefix):
        return [(k, v) for k, v in sorted(self.objects.items())
                if k.startswith(prefix)]

    def delete_object(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


def quiet(call, *args, **kwargs):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = call(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


def parse(*argv):
    args = rig.build_parser().parse_args(list(argv))
    rig.check_arguments(args)
    return args


def make_rig(es, names=None, s3=None, s3_reason="no s3", base_path="churnrig"):
    return rig.Rig(es, names or rig.names("churnrig"), s3, s3_reason,
                   base_path)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = lambda name: os.path.join(self.dir.name, name)


class ReportShowsWhatTheRepositoryIsLeaking(unittest.TestCase):

    def test_blobs_of_expired_snapshots_are_counted_as_leaked(self):
        # The leak count is the number this rig exists to produce. If a
        # snapshot metadata blob that no live snapshot owns stopped being
        # counted, the reclaim tooling would be measured against a repository
        # that looks clean when it is not.
        s3 = FakeS3({"churnrig/snap-live.dat": 10,
                     "churnrig/snap-gone.dat": 20,
                     "churnrig/index-3": 5,
                     "churnrig/index-4": 5,
                     "churnrig/index.latest": 8})
        section = rig.repository_section(
            make_rig(FakeEs(), s3=s3), {"live"})
        self.assertEqual(
            (section["snapshot_metadata_blobs"],
             section["expired_snapshot_metadata_still_present"],
             section["root_generations"], section["index_latest_present"],
             section["bytes"]),
            (2, 1, [3, 4], True, 48))

    def test_shard_directories_and_other_base_paths_are_not_counted_as_root(
            self):
        # A key with a slash after the prefix is shard data, and a sibling
        # base path is another repository. Counting either as root metadata
        # would report an index-N or snap-*.dat that is not ours, and the
        # run would announce a leak that another tenant's repository caused.
        s3 = FakeS3({"churnrig/snap-x/0/y.dat": 1,
                     "churnrig-other/index-9": 1,
                     "churnrig/index-1": 1})
        section = rig.repository_section(make_rig(FakeEs(), s3=s3), set())
        self.assertEqual(
            (section["root_generations"],
             section["snapshot_metadata_blobs"]), ([1], 0))

    def test_root_generations_report_only_the_newest_eight(self):
        # An unbounded list would make every report line grow for as long as
        # the leak does, and the report file is read by people and scripts
        # that expect one short line per interval.
        s3 = FakeS3({"churnrig/index-%d" % i: 1 for i in range(20)})
        section = rig.repository_section(make_rig(FakeEs(), s3=s3), set())
        self.assertEqual(section["root_generations"], list(range(12, 20)))
        self.assertEqual(section["root_generation_count"], 20)

    def test_an_empty_base_path_is_refused_rather_than_read_as_the_bucket(
            self):
        # The rig never registers a repository at the bucket root, so an
        # empty base path is a mistake, and listing the whole bucket would
        # count another repository's index-N and snap-*.dat as this rig's
        # leak. Models a hand-edited state file or an empty template value.
        s3 = FakeS3({"index-2": 1, "gcw/index-7": 1})
        for empty in ("", "/", None):
            with self.subTest(base_path=empty):
                with self.assertRaises(SystemExit):
                    quiet(rig.repository_section,
                          make_rig(FakeEs(), s3=s3, base_path=empty), set())

    def test_a_missing_s3_is_reported_with_its_reason_not_as_empty(self):
        # An operator who forgot the S3 credentials must read why there is no
        # repository section, not a zero that looks like a clean bucket.
        es = FakeEs()
        report = rig.gather_report(make_rig(es, s3=None,
                                            s3_reason="no --s3-endpoint"))
        self.assertEqual(report["repository"],
                         {"unavailable": "no --s3-endpoint"})

    def test_gathered_report_counts_live_snapshots_against_the_bucket(self):
        # The report joins what Elasticsearch lists with what the bucket
        # holds. If the uuid join broke, every snapshot blob would read as
        # leaked, or none would.
        n = rig.names("churnrig")
        es = FakeEs()
        es.present[rig.SNAPSHOTS_IN_REPO_PATH % n["repo"]] = {"snapshots": [
            {"snapshot": "churnrig-snap-1", "uuid": "u1", "state": "SUCCESS",
             "start_time_in_millis": 1000}]}
        s3 = FakeS3({"churnrig/snap-u1.dat": 1, "churnrig/snap-u0.dat": 1})
        report = rig.gather_report(make_rig(es, s3=s3))
        self.assertEqual(
            (report["snapshots"]["alive"],
             report["repository"]["expired_snapshot_metadata_still_present"]),
            (1, 1))


class ReportShowsTheClusterSide(unittest.TestCase):

    def test_a_snapshot_listed_under_the_old_field_name_still_counts(self):
        # Older responses call the field name, not snapshot. Missing it would
        # blank the cadence and the mount-hazard check on those clusters.
        self.assertEqual(rig.snap_name({"name": "a"}), "a")
        self.assertEqual(rig.snap_name({"snapshot": "b", "name": "a"}), "b")
        self.assertEqual(rig.snap_name({}), "")

    def test_data_stream_section_is_none_once_the_stream_is_gone(self):
        # The run loop reads a missing stream as "backing index vanished". A
        # crash here instead would end the run at the moment it matters.
        rg = make_rig(FakeEs())
        self.assertIsNone(rig.data_stream_section(rg))
        rg.es.present[rig.DATA_STREAM_PATH + "churnrig-stream"] = {
            "data_streams": []}
        self.assertIsNone(rig.data_stream_section(rg))

    def test_data_stream_section_names_backing_indices_and_generation(self):
        # Rollover detection reads the backing index count from this.
        rg = make_rig(FakeEs())
        rg.es.present[rig.DATA_STREAM_PATH + "churnrig-stream"] = {
            "data_streams": [{"generation": 2, "indices": [
                {"index_name": ".ds-a-1"}, {"index_name": ".ds-a-2"}]}]}
        self.assertEqual(rig.data_stream_section(rg),
                         {"backing_indices": 2, "generation": 2,
                          "backing_index_names": [".ds-a-1", ".ds-a-2"]})

    def test_ilm_errors_are_listed_and_phases_counted(self):
        # An index stuck on ERROR stops the lifecycle that creates the leak
        # being measured. If it were not surfaced, a stalled rig would look
        # like a healthy quiet one.
        rg = make_rig(FakeEs())
        rg.es.present["/churnrig-stream/_ilm/explain"] = {"indices": {
            "a": {"phase": "hot"}, "b": {"phase": "hot"},
            "c": {"phase": "frozen", "step": "ERROR", "failed_step": "mount"},
            "d": {}}}
        phases, errors = rig.ilm_section(rg)
        self.assertEqual(phases, {"hot": 2, "frozen": 1, "unmanaged": 1})
        self.assertEqual(errors, [{"index": "c", "failed_step": "mount"}])

    def test_a_mount_whose_snapshot_expired_is_a_hazard(self):
        # This is the state the reclaim tooling is measured against: an index
        # serving reads from blobs no snapshot references. If it stopped being
        # reported, the harness would stop saying when it had made one.
        mounted = [
            {"index": "m1", "snapshot": "gone", "repository": "r"},
            {"index": "m2", "snapshot": "alive", "repository": "r"},
            {"index": "m3", "snapshot": "gone", "repository": "elsewhere"}]
        hazards = rig.mount_hazards(mounted, "r", {"alive"})
        self.assertEqual([h["index"] for h in hazards], ["m1"])

    def test_mounted_indices_read_the_snapshot_each_one_serves_from(self):
        # Hazard detection compares these names against the repository's
        # listing. A mis-read name would flag every mount, or none.
        rg = make_rig(FakeEs())
        path = ("/*churnrig*/_settings?expand_wildcards=all&filter_path="
                "*.settings.index.store.snapshot")
        rg.es.present[path] = {
            "partial-x": {"settings": {"index": {"store": {"snapshot": {
                "snapshot_name": "s", "repository_name": "r",
                "partial": "true"}}}}},
            "plain": {"settings": {"index": {}}}}
        self.assertEqual(rig.mounted_indices(rg, "churnrig"),
                         [{"index": "partial-x", "snapshot": "s",
                           "repository": "r", "partial": "true"}])

    def test_cadence_survives_snapshots_the_repository_has_expired(self):
        # Retention deletes old snapshots, so a long run would see its own
        # cadence shrink to the retention window. The memory keeps the
        # starts, so the reported spacing stays the real one.
        rg = make_rig(FakeEs())
        memory = {}
        rig.observed_start_deltas(rg, [
            {"snapshot": "churnrig-snap-1", "start_time_in_millis": 0},
            {"snapshot": "churnrig-snap-2", "start_time_in_millis": 60000}],
            memory)
        deltas = rig.observed_start_deltas(rg, [
            {"snapshot": "churnrig-snap-3", "start_time_in_millis": 150000},
            {"snapshot": "foreign-snap", "start_time_in_millis": 1}], memory)
        self.assertEqual(deltas, [60.0, 90.0])

    def test_snapshot_section_reads_slm_counters_and_states(self):
        # The expired and deletion-failure counters are what the milestones
        # key on. Reading the wrong stats key would hide the first failed
        # delete, the event the rig is built to provoke.
        rg = make_rig(FakeEs())
        rg.es.present["/_slm/policy/churnrig-slm"] = {"churnrig-slm": {
            "stats": {"snapshots_taken": 5, "snapshots_failed": 1,
                      "snapshots_deleted": 3,
                      "snapshot_deletion_failures": 2}}}
        rg.es.present["/_slm/stats"] = {"retention_runs": 4,
                                        "retention_failed": 1}
        section = rig.snapshot_section(rg, [
            {"snapshot": "churnrig-snap-1", "state": "SUCCESS",
             "start_time_in_millis": 0},
            {"snapshot": "churnrig-snap-2", "state": "PARTIAL",
             "start_time_in_millis": 1000}], {})
        self.assertEqual(
            (section["expired_total"], section["snapshot_deletion_failures"],
             section["alive_by_state"], section["retention_failed"]),
            (3, 2, {"SUCCESS": 1, "PARTIAL": 1}, 1))


class RunStateCountsWhatItSendsAndWhatItLoses(TempDirCase):

    def state(self, es=None, **overrides):
        args = parse("run", "--es", "http://x:9200", "--docs-per-second", "3",
                     "--report-file", self.path("r.jsonl"),
                     "--state-file", self.path("s.json"))
        for key, value in overrides.items():
            setattr(args, key, value)
        return rig.RunState(make_rig(es or FakeEs()), args)

    def test_a_failed_bulk_counts_every_document_as_an_error(self):
        # An outage that drops the whole batch must show in bulk_errors. If
        # it counted none, a rig ingesting nothing would report zero errors.
        es = FakeEs()
        es.bulk_error = rig.EsError(503, "down", "http://x")
        state = self.state(es)
        quiet(state.ingest)
        self.assertEqual((state.docs_sent, state.bulk_errors), (0, 3))

    def test_only_rejected_items_count_when_a_bulk_partly_fails(self):
        # Counting the whole batch on a partial rejection would overstate
        # the error rate and hide which documents landed.
        es = FakeEs()
        es.bulk_response = {"errors": True, "items": [
            {"create": {"status": 201}}, {"create": {"status": 429}},
            {"create": {}}]}
        state = self.state(es)
        state.ingest()
        self.assertEqual((state.docs_sent, state.bulk_errors), (3, 1))

    def test_zero_rate_sends_nothing(self):
        # --docs-per-second 0 is how an operator watches the leak without
        # adding to it. Posting an empty bulk would be rejected by the
        # cluster and counted as an outage.
        es = FakeEs()
        state = self.state(es, docs_per_second=0)
        state.ingest()
        self.assertEqual(es.calls, [])

    def test_a_frozen_mount_is_not_a_deleted_backing_index(self):
        # A mount renames .ds-X to partial-.ds-X. Reading that as a deletion
        # would fire first_backing_index_deleted on every healthy mount and
        # make the milestone meaningless.
        state = self.state()
        rep = {"data_stream": {"backing_index_names": [".ds-a-1", ".ds-a-2"]}}
        state.note_backing_index_loss(rep)
        rep = {"data_stream": {"backing_index_names": [
            "partial-.ds-a-1", ".ds-a-2", ".ds-a-3"]}}
        state.note_backing_index_loss(rep)
        self.assertNotIn("backing_index_disappeared", rep)

    def test_a_backing_index_that_vanishes_is_marked(self):
        # ILM deleting an index it should have kept is one of the events
        # under test. If the mark were lost, the milestone could not fire.
        state = self.state()
        state.note_backing_index_loss(
            {"data_stream": {"backing_index_names": [".ds-a-1", ".ds-a-2"]}})
        rep = {"data_stream": {"backing_index_names": [".ds-a-2"]}}
        state.note_backing_index_loss(rep)
        self.assertTrue(rep["backing_index_disappeared"])

    def test_a_missing_stream_does_not_crash_the_loss_check(self):
        # The stream can be deleted mid-run. The check must see no names, not
        # raise and end the run.
        state = self.state()
        state.note_backing_index_loss({"data_stream": None})

    def test_each_milestone_records_only_the_first_time(self):
        # The milestone timestamps are the run's headline numbers. A later
        # observation overwriting the first would report when the condition
        # last held, not when it first happened.
        state = self.state()
        rep = {"data_stream": {"backing_indices": 2},
               "mounted_searchable": {"count": 0, "hazards": []},
               "snapshots": {"alive_by_state": {}, "expired_total": 0,
                             "snapshot_deletion_failures": 0},
               "repository": {"unavailable": "x"}}
        quiet(state.note_milestones, rep, 10.0)
        quiet(state.note_milestones, rep, 99.0)
        self.assertEqual(state.milestones, {"first_rollover": 10.0})

    def test_repository_milestones_wait_for_a_real_repository_section(self):
        # With no S3 listing the repository section is a reason string. A
        # milestone that indexed into it would crash every poll.
        reached = dict(rig.MILESTONES)
        base = {"data_stream": None,
                "mounted_searchable": {"count": 0, "hazards": []},
                "snapshots": {"alive_by_state": {}, "expired_total": 0,
                              "snapshot_deletion_failures": 0}}
        rep = dict(base, repository={"unavailable": "x"})
        self.assertFalse(reached["first_leaked_root_generation"](rep))
        self.assertFalse(reached["first_leaked_snapshot_metadata"](rep))
        rep = dict(base, repository={
            "root_generation_count": 3,
            "expired_snapshot_metadata_still_present": 1})
        self.assertTrue(reached["first_leaked_root_generation"](rep))
        self.assertTrue(reached["first_leaked_snapshot_metadata"](rep))

    def test_observe_logs_each_mount_hazard(self):
        # The hazard line in the log is what an operator watching the run
        # sees first. Without it the state is only in the JSON.
        n = rig.names("churnrig")
        es = FakeEs()
        es.present[rig.SNAPSHOTS_IN_REPO_PATH % n["repo"]] = {"snapshots": []}
        es.present["/*churnrig*/_settings?expand_wildcards=all&filter_path="
                   "*.settings.index.store.snapshot"] = {
            "partial-x": {"settings": {"index": {"store": {"snapshot": {
                "snapshot_name": "gone", "repository_name": n["repo"]}}}}}}
        state = self.state(es)
        rep, _, err = quiet(state.observe, 5.0)
        self.assertIn("HAZARD", err)
        self.assertIn("first_mount_hazard", state.milestones)

    def test_emit_prints_and_appends_one_json_line(self):
        # Reports are consumed as JSON lines from stdout and from the file.
        # A second emit must append, or a long run keeps only its last line.
        state = self.state()
        state.milestones["first_rollover"] = 4.04
        _, out, _ = quiet(state.emit, {"ts": "t"})
        quiet(state.emit, {"ts": "t2"})
        lines = pathlib.Path(self.path("r.jsonl")).read_text().splitlines()
        self.assertEqual(json.loads(out)["milestones"],
                         {"first_rollover": 4.0})
        self.assertEqual(len(lines), 2)

    def test_a_failed_report_poll_is_logged_and_the_run_continues(self):
        # A transient cluster error during a poll must not end a run that is
        # meant to churn for hours. If the exception escaped, the standing
        # churn would stop being exercised until someone noticed.
        state = self.state()
        calls = []

        def flaky(now):
            calls.append(now)
            raise rig.EsError(500, "boom", "http://x")

        state.observe = flaky
        state.ingest = lambda: None
        ticks = iter(range(100))
        with mock.patch.object(rig.time, "sleep"), \
                mock.patch.object(type(state), "elapsed",
                                  new_callable=mock.PropertyMock,
                                  side_effect=lambda: next(ticks) * 1.0):
            _, _, err = quiet(rig.churn, state, 3.0, 1.0)
        self.assertIn("report poll failed, continuing", err)
        self.assertGreaterEqual(len(calls), 2)


class RunCommand(TempDirCase):

    def args(self, *extra):
        return parse("run", "--es", "http://x:9200",
                     "--state-file", self.path("s.json"),
                     "--report-file", self.path("r.jsonl"),
                     "--bucket", "b", *extra)

    def test_a_leftover_state_file_stops_the_run_before_any_write(self):
        # The state file holds the settings the last run changed. Starting
        # over it would record the already-changed values as the originals,
        # and teardown would then restore the wrong settings.
        pathlib.Path(self.path("s.json")).write_text("{}")
        es = FakeEs()
        with self.assertRaises(SystemExit) as raised:
            quiet(rig.cmd_run, es, self.args(), rig.names("churnrig"),
                  None, "x")
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(es.calls, [])

    def test_an_interrupt_still_writes_the_final_report(self):
        # Ctrl-C is how an operator ends a run early. The final report is the
        # only record of what the run saw, so losing it on interrupt wastes
        # the run.
        es = FakeEs()
        with mock.patch.object(rig, "churn", side_effect=KeyboardInterrupt):
            code, out, err = quiet(rig.cmd_run, es, self.args(),
                                   rig.names("churnrig"), None, "no s3")
        self.assertEqual(code, 0)
        self.assertIn("interrupted", err)
        self.assertEqual(json.loads(out)["repository"],
                         {"unavailable": "no s3"})

    def test_run_registers_the_rig_then_reports_when_the_duration_ends(self):
        # End to end through setup, a zero-length churn and the final
        # report. If setup stopped writing the state file the run would
        # change cluster settings with no record to restore them from.
        es = FakeEs()
        code, out, _ = quiet(rig.cmd_run, es, self.args("--duration", "0s"),
                             rig.names("churnrig"), None, "no s3")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.path("s.json")))
        self.assertIn("/_snapshot/churnrig-repo", es.paths("PUT"))


class SetupDecisions(TempDirCase):

    def args(self, *extra):
        return parse("run", "--es", "http://x:9200",
                     "--state-file", self.path("s.json"),
                     "--bucket", "b", *extra)

    def setup(self, es, *extra):
        args = self.args(*extra)
        return quiet(rig.cmd_setup, es, args, rig.names("churnrig"), None)

    def test_a_cluster_older_than_7_12_is_refused_untouched(self):
        # The frozen tier does not exist there. Carrying on would change
        # settings and then fail at the first frozen mount.
        es = FakeEs(version="7.10.2")
        with self.assertRaises(SystemExit):
            self.setup(es)
        self.assertEqual(es.calls, [])

    def test_a_colliding_prefix_is_refused_before_any_write(self):
        # Another tenant's index under the prefix would be adopted by the
        # rig and then deleted by teardown's wildcard.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % "churnrig"] = {
            "indices": [{"name": "churnrig-foreign"}],
            "aliases": [{"name": "churnrig-alias"}],
            "data_streams": [{"name": "churnrig-ds"}]}
        es.present[rig.ILM_POLICY_PATH + "churnrig-ilm"] = {"x": 1}
        es.present[rig.SLM_POLICY_PATH + "churnrig-slm"] = {"x": 1}
        es.present[rig.SNAPSHOT_PATH + "churnrig-repo"] = {"x": 1}
        es.present[rig.INDEX_TEMPLATE_PATH + "churnrig-template"] = {"x": 1}
        with self.assertRaises(SystemExit):
            self.setup(es)
        self.assertEqual(es.calls, [])

    def test_a_missing_frozen_node_is_refused(self):
        # No data_frozen node means the frozen phase can never mount, so the
        # rig would churn without exercising the deletes it exists for.
        es = FakeEs()
        es.nodes = {"n": {"roles": ["data_hot"]}}
        with self.assertRaises(SystemExit):
            self.setup(es)

    def test_a_frozen_node_without_a_shared_cache_is_refused(self):
        # A shared cache of zero cannot mount partial snapshots. The rig
        # would build a policy that errors at the frozen step.
        es = FakeEs()
        es.nodes = {"n": {"roles": ["data_hot", "data_frozen"], "settings": {
            "xpack": {"searchable": {"snapshot": {
                "shared_cache": {"size": "0b"}}}}}}}
        with self.assertRaises(SystemExit):
            self.setup(es)

    def test_a_dedicated_frozen_node_defaults_to_a_usable_cache(self):
        # A dedicated frozen node gets a default cache with no setting. If
        # that were refused, the common lab layout could not run the rig.
        es = FakeEs()
        es.nodes = {"n": {"roles": ["data_frozen"]}}
        self.setup(es)
        self.assertIn("/_snapshot/churnrig-repo", es.paths("PUT"))

    def test_the_slm_minimum_is_lowered_only_when_the_cadence_needs_it(self):
        # Lowering a cluster-wide minimum the rig does not need would change
        # other tenants' policies. Teardown restores what is recorded here.
        es = FakeEs()
        self.setup(es, "--snapshot-interval", "5m")
        changed = json.loads(pathlib.Path(self.path("s.json")).read_text())[
            "settings_changed"]
        self.assertIn(rig.SLM_MINIMUM_INTERVAL, changed)
        os.unlink(self.path("s.json"))
        es = FakeEs()
        self.setup(es, "--snapshot-interval", "20m")
        changed = json.loads(pathlib.Path(self.path("s.json")).read_text())[
            "settings_changed"]
        self.assertNotIn(rig.SLM_MINIMUM_INTERVAL, changed)

    def test_an_s3_repository_needs_a_bucket(self):
        # A repository registered with no bucket would fail on the cluster
        # with a message about settings, after cluster settings were changed.
        args = self.args("--repo-type", "s3")
        args.bucket = None
        with self.assertRaises(SystemExit):
            quiet(rig.cmd_setup, FakeEs(), args, rig.names("churnrig"), None)

    def test_an_fs_repository_needs_a_location_and_uses_it(self):
        # fs has no bucket to fall back on. With a location it registers a
        # filesystem repository, and the S3 settings do not leak into it.
        args = self.args("--repo-type", "fs")
        args.location = None
        with self.assertRaises(SystemExit):
            quiet(rig.cmd_setup, FakeEs(), args, rig.names("churnrig"), None)
        args.location = "/mnt/snaps"
        es = FakeEs()
        os.unlink(self.path("s.json"))
        quiet(rig.cmd_setup, es, args, rig.names("churnrig"), None)
        body = [b for _, p, b in es.calls
                if p == "/_snapshot/churnrig-repo"][0]
        self.assertEqual(body, {"type": "fs",
                                "settings": {"location": "/mnt/snaps"}})

    def test_a_store_that_rejects_the_verify_delete_is_registered_unverified(
            self):
        # The store this project studies rejects batch deletes, and
        # registration verifies by batch-deleting. Without the retry the rig
        # could never register on exactly the stores it targets, and the
        # state file would not record that the first evidence was seen.
        es = FakeEs()
        es.put_failures["/_snapshot/churnrig-repo"] = rig.EsError(
            500, "cannot delete test data at [x]", "u")
        self.setup(es)
        state = json.loads(pathlib.Path(self.path("s.json")).read_text())
        self.assertTrue(state["verify_rejected_batch_delete"])
        self.assertIn("/_snapshot/churnrig-repo?verify=false",
                      es.paths("PUT"))

    def test_any_other_registration_failure_is_not_swallowed(self):
        # Retrying without verification on every error would register a
        # repository with bad credentials and report the rig as running.
        es = FakeEs()
        es.put_failures["/_snapshot/churnrig-repo"] = rig.EsError(
            403, "access denied", "u")
        with self.assertRaises(rig.EsError):
            self.setup(es)
        self.assertNotIn("/_snapshot/churnrig-repo?verify=false",
                         es.paths("PUT"))

    def test_an_old_cluster_gets_a_cron_schedule_for_an_even_cadence(self):
        # Before 8.14 SLM takes only cron. A cadence that does not divide the
        # hour would silently run at the wrong interval, so it is refused.
        self.assertEqual(rig.slm_schedule((8, 13), "15m", None),
                         "0 0/15 * * * ?")
        with self.assertRaises(SystemExit):
            quiet(rig.slm_schedule, (8, 13), "7m", None)
        self.assertEqual(rig.slm_schedule((8, 13), "7m", "0 0 * * * ?"),
                         "0 0 * * * ?")


class StateFile(TempDirCase):

    def test_an_unwritable_state_file_stops_setup(self):
        # Settings changed with no record of the originals cannot be put
        # back. The run has to stop before it changes them.
        with self.assertRaises(SystemExit) as raised:
            quiet(rig.write_state, self.path("missing/dir/s.json"), {})
        self.assertEqual(raised.exception.code, 2)

    def test_a_state_path_that_is_a_directory_is_refused_with_the_reason(self):
        # The operator needs to know which path is wrong and why, not read a
        # traceback.
        os.mkdir(self.path("s.json"))
        _, _, err = self.refusal(rig.write_state, self.path("s.json"), {})
        self.assertIn("is a directory", err)

    def refusal(self, call, *args):
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            with self.assertRaises(SystemExit):
                call(*args)
        return None, "", captured.getvalue()

    def test_a_corrupt_state_file_is_refused_not_treated_as_absent(self):
        # Falling back to prefix-derived names would restore no settings and
        # widen what teardown touches. Refusing keeps the file for repair.
        pathlib.Path(self.path("s.json")).write_text("{not json")
        with self.assertRaises(SystemExit) as raised:
            quiet(rig.load_state, self.path("s.json"))
        self.assertEqual(raised.exception.code, 2)

    def test_a_written_state_round_trips(self):
        # Teardown restores from exactly what setup wrote.
        quiet(rig.write_state, self.path("s.json"), {"prefix": "p"})
        self.assertEqual(rig.load_state(self.path("s.json")),
                         {"prefix": "p"})

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 0200 file")
    def test_an_unreadable_secret_file_names_the_error_not_the_contents(self):
        # The message is printed and logged. The secret must never be in it.
        path = self.path("secret")
        pathlib.Path(path).write_text("hunter2")
        os.chmod(path, stat.S_IWUSR)
        root = {"GENCHAIN_SECRET_ROOT": os.path.dirname(path)}
        with mock.patch.dict(os.environ, root):
            _, _, err = self.refusal(rig.read_secret_file, path,
                                     "--password-file")
        self.assertNotIn("hunter2", err)
        self.assertIn("PermissionError", err)


class MakeS3ExplainsWhyThereIsNoRepositorySection(unittest.TestCase):

    def args(self, *extra):
        return parse("status", "--es", "http://x:9200", *extra)

    def test_each_missing_piece_gets_its_own_reason(self):
        # The report prints this reason in place of the repository section.
        # A vague one sends the operator hunting for the wrong flag.
        env = {k: v for k, v in os.environ.items()
               if k not in ("S3_ACCESS_KEY", "S3_SECRET_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIn("--s3-endpoint", rig.make_s3(self.args())[1])
            s3, reason = rig.make_s3(self.args(
                "--s3-endpoint", "http://s3:9000"))
            self.assertIsNone(s3)
            self.assertIn("credentials missing", reason)
            s3, reason = rig.make_s3(self.args(
                "--s3-endpoint", "http://s3:9000", "--s3-access-key", "a"))
            self.assertIn("credentials missing", reason)
        with mock.patch.dict(os.environ, {"S3_SECRET_KEY": "s"}):
            s3, reason = rig.make_s3(self.args(
                "--s3-endpoint", "http://s3:9000", "--s3-access-key", "a"))
        self.assertIn("no --bucket", reason)

    def test_a_complete_s3_configuration_builds_a_lister(self):
        # If the happy path stopped returning a client, every report would
        # silently drop its repository section.
        with mock.patch.dict(os.environ, {"S3_SECRET_KEY": "s"}):
            s3, reason = rig.make_s3(self.args(
                "--s3-endpoint", "http://s3:9000", "--s3-access-key", "a",
                "--bucket", "b"))
        self.assertIsNone(reason)
        self.assertEqual(s3.bucket, "b")

    def test_a_secret_key_file_is_read_through_the_mode_check(self):
        # The store key gets the same refusal as the password. A group
        # readable key file must not be accepted here.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "k")
            pathlib.Path(path).write_text("s\n")
            os.chmod(path, 0o644)
            with self.assertRaises(SystemExit):
                quiet(rig.make_s3, self.args(
                    "--s3-endpoint", "http://s3:9000", "--s3-access-key",
                    "a", "--bucket", "b", "--s3-secret-key-file", path))


class RedirectRefusalNamesOnlyTheHost(unittest.TestCase):

    def test_an_unparseable_location_is_not_echoed(self):
        # The message is logged. A Location that cannot be parsed may carry a
        # credential in its query, so it is described and never quoted.
        err = rig.RedirectRefused(302, "http://[bad/?token=secret")
        self.assertNotIn("secret", str(err))
        self.assertIn("unreadable", str(err))

    def test_an_ipv6_host_keeps_its_brackets_and_port(self):
        # Without the brackets the host and port run together and the
        # operator cannot tell where the credential nearly went.
        err = rig.RedirectRefused(301, "http://[::1]:9200/x?token=secret")
        self.assertIn("[::1]:9200", str(err))
        self.assertNotIn("secret", str(err))


class TeardownRemovesOnlyWhatTheRigMade(TempDirCase):

    def names(self):
        return rig.names("churnrig")

    def test_snapshots_are_deleted_ten_to_a_request(self):
        # One request per snapshot is slow and one for all of them is a URL
        # the cluster rejects. Batching by ten is what makes a long run's
        # worth of snapshots deletable at all.
        n = self.names()
        es = FakeEs()
        es.present[rig.SNAPSHOTS_IN_REPO_PATH % n["repo"]] = {"snapshots": [
            {"snapshot": "s%d" % i} for i in range(23)]}
        quiet(rig.delete_cluster_objects, es, n)
        batches = [p for p in es.paths("DELETE")
                   if p.startswith("/_snapshot/churnrig-repo/")]
        self.assertEqual([len(p.rsplit("/", 1)[1].split(",")) for p in batches],
                         [10, 10, 3])
        self.assertEqual(es.paths("DELETE")[-1], "/_snapshot/churnrig-repo")

    def test_a_repository_that_is_already_gone_is_left_alone(self):
        # A second teardown after a partial first one must not fail on the
        # missing repository.
        n = self.names()
        es = FakeEs()
        quiet(rig.delete_cluster_objects, es, n)
        self.assertNotIn("/_snapshot/churnrig-repo", es.paths("DELETE"))

    def test_leftover_indices_are_deleted_only_when_they_are_ours(self):
        # The wildcard resolves other tenants' indices too. Deleting one
        # destroys data this project's recovery path runs through.
        n = self.names()
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % "churnrig-stream"] = {"indices": [
            {"name": "partial-.ds-churnrig-stream-2026.01.01-000001"},
            {"name": "churnrig-stream-someone-elses"}]}
        quiet(rig.delete_cluster_objects, es, n)
        self.assertIn("/partial-.ds-churnrig-stream-2026.01.01-000001",
                      es.paths("DELETE"))
        self.assertNotIn("/churnrig-stream-someone-elses", es.paths("DELETE"))

    def test_settings_are_restored_to_the_recorded_values_only(self):
        # Restoring a setting the rig never changed would overwrite an
        # operator's own value. None puts a setting back to its default.
        es = FakeEs()
        state = {"prior_settings": {rig.ILM_POLL_INTERVAL: "10m",
                                    rig.SLM_RETENTION_SCHEDULE: None,
                                    rig.SLM_MINIMUM_INTERVAL: "15m"},
                 "settings_changed": {rig.ILM_POLL_INTERVAL: "1m",
                                      rig.SLM_RETENTION_SCHEDULE: "x"}}
        quiet(rig.restore_managed_settings, es, state)
        self.assertEqual(es.calls[0][2], {"persistent": {
            rig.ILM_POLL_INTERVAL: "10m", rig.SLM_RETENTION_SCHEDULE: None}})

    def test_a_setting_that_did_not_come_back_is_reported(self):
        # A restore that the cluster silently ignored would leave ILM polling
        # every second forever. The verdict must say so.
        es = FakeEs()
        es.persistent = {rig.ILM_POLL_INTERVAL: "1m"}
        state = {"prior_settings": {rig.ILM_POLL_INTERVAL: "10m"},
                 "settings_changed": {rig.ILM_POLL_INTERVAL: "1m"}}
        self.assertEqual(rig.settings_not_restored(es, state),
                         {rig.ILM_POLL_INTERVAL: {"expected": "10m",
                                                  "actual": "1m"}})
        es.persistent = {rig.ILM_POLL_INTERVAL: "10m"}
        self.assertEqual(rig.settings_not_restored(es, state), {})

    def test_residue_of_every_kind_makes_the_verdict_unclean(self):
        # "clean" is what lets teardown delete the state file. A verdict that
        # missed a surviving policy or index would delete the only record of
        # what to restore.
        n = self.names()
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % "churnrig"] = {
            "indices": [{"name": "churnrig-x"}],
            "aliases": [{"name": "a"}], "data_streams": [{"name": "d"}]}
        for path in (rig.ILM_POLICY_PATH + n["ilm"],
                     rig.SLM_POLICY_PATH + n["slm"],
                     rig.SNAPSHOT_PATH + n["repo"],
                     rig.INDEX_TEMPLATE_PATH + n["template"]):
            es.present[path] = {"x": 1}
        verdict = rig.teardown_verdict(es, parse(
            "teardown", "--es", "http://x"), n, None)
        self.assertFalse(verdict["clean"])
        for name in ("churnrig-x", "a", "d", n["ilm"], n["slm"], n["repo"],
                     n["template"]):
            with self.subTest(name=name):
                self.assertTrue(any(entry.endswith(" " + name)
                                    for entry in verdict["remaining"]))

    def test_a_cluster_with_nothing_left_is_clean(self):
        # The reverse: a false "unclean" would keep the state file forever
        # and block the next run.
        es = FakeEs()
        verdict = rig.teardown_verdict(es, parse(
            "teardown", "--es", "http://x"), self.names(), None)
        self.assertTrue(verdict["clean"])
        self.assertNotIn("remaining", verdict)


class AChosenStreamNameIsCheckedAndCleanedUp(TempDirCase):
    """--data-stream names a stream that need not contain the prefix.

    Preflight, the leftover sweep and the residue check all have to look at
    that name itself, or the override becomes a blind spot in each of them.
    """

    STREAM = "team-metrics-test"
    ORPHAN = "partial-.ds-team-metrics-test-2026.10.05-000001"

    def names(self):
        return rig.names("octest", data_stream=self.STREAM)

    def teardown_args(self):
        return parse("teardown", "--es", "http://x", "--prefix", "octest",
                     "--data-stream", self.STREAM)

    def test_setup_refuses_a_stream_that_already_exists(self):
        # Setup would install a priority-500 template with a delete phase
        # over another team's stream and record the stream as the rig's own.
        # The teardown that follows the failed run then deletes that stream
        # and every document in it.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % self.STREAM] = {
            "data_streams": [{"name": self.STREAM}]}
        args = parse("run", "--es", "http://x:9200", "--prefix", "octest",
                     "--data-stream", self.STREAM, "--bucket", "b",
                     "--state-file", self.path("s.json"))
        with self.assertRaises(SystemExit):
            quiet(rig.cmd_setup, es, args, self.names(), None)
        self.assertEqual(es.calls, [])
        self.assertFalse(os.path.exists(self.path("s.json")))

    def test_setup_proceeds_when_nothing_answers_to_the_stream(self):
        # A preflight that refused every override would make --data-stream
        # unusable on the clusters it exists for.
        es = FakeEs()
        args = parse("run", "--es", "http://x:9200", "--prefix", "octest",
                     "--data-stream", self.STREAM, "--bucket", "b",
                     "--state-file", self.path("s.json"))
        quiet(rig.cmd_setup, es, args, self.names(), None)
        self.assertIn(rig.DATA_STREAM_PATH + self.STREAM, es.paths("PUT"))

    def test_teardown_sweeps_the_frozen_mounts_of_the_chosen_stream(self):
        # A frozen mount the sweep cannot see pins snapshots after teardown
        # reports clean, and the operator believes the rig is gone. Under an
        # override the stream's indices never contain the prefix.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % self.STREAM] = {"indices": [
            {"name": self.ORPHAN},
            {"name": "partial-.ds-x-team-metrics-test-2026.10.05-000001"}]}
        quiet(rig.delete_cluster_objects, es, self.names())
        self.assertIn("/" + self.ORPHAN, es.paths("DELETE"))
        self.assertNotIn(
            "/partial-.ds-x-team-metrics-test-2026.10.05-000001",
            es.paths("DELETE"))

    def test_a_surviving_index_under_the_stream_name_is_residue(self):
        # A bulk request that lands after the template is gone recreates the
        # stream name as a plain index. A verdict blind to it reports clean
        # and removes the state file while the rig's data is still there.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % self.STREAM] = {
            "indices": [{"name": self.STREAM}]}
        verdict = rig.teardown_verdict(
            es, self.teardown_args(), self.names(), None)
        self.assertFalse(verdict["clean"])

    def test_a_lookalike_under_the_stream_name_is_not_residue(self):
        # Another tenant's index that only contains the stream name would
        # keep the verdict unclean forever, and the state file would block
        # every later run.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % self.STREAM] = {
            "indices": [{"name": "team-metrics-test-prod"}]}
        verdict = rig.teardown_verdict(
            es, self.teardown_args(), self.names(), None)
        self.assertTrue(verdict["clean"])

    def test_residue_under_the_recorded_prefix_is_seen_when_flags_differ(self):
        # The chart's stale-state teardown passes the current --prefix with
        # a state file an earlier run wrote under another prefix. Checking
        # only the current prefix reports clean over the earlier rig's
        # leftovers and deletes the only record of them.
        es = FakeEs()
        es.present[rig.RESOLVE_PREFIX_PATH % "leaktest"] = {
            "indices": [{"name": "leaktest-leftover"}]}
        state = {"prefix": "leaktest", "prior_settings": {},
                 "settings_changed": {}}
        verdict = rig.teardown_verdict(
            es, parse("teardown", "--es", "http://x", "--prefix", "newrig"),
            rig.names("leaktest"), state)
        self.assertFalse(verdict["clean"])


class BucketClearing(unittest.TestCase):

    def test_without_the_purge_flag_nothing_is_deleted(self):
        # The leaked corpus is the measurement target and the objects do not
        # come back. Deleting without being asked destroys it.
        s3 = FakeS3({"churnrig/a": 1, "churnrig/b": 1})
        self.assertEqual(quiet(rig.clear_bucket, s3, "churnrig", False)[0],
                         (0, 2))
        self.assertEqual(s3.deleted, [])

    def test_an_empty_bucket_prints_no_residue_warning(self):
        # A false residue warning would send the operator looking for blobs
        # that are not there.
        s3 = FakeS3()
        result, _, err = quiet(rig.clear_bucket, s3, "churnrig", False)
        self.assertEqual(result, (0, 0))
        self.assertNotIn("remain", err)

    def test_purge_deletes_one_object_at_a_time_under_the_base_path_only(self):
        # The batch delete is the call the store drops, so purge uses single
        # deletes. A sibling base path is another repository and must stay.
        s3 = FakeS3({"churnrig/a": 1, "churnrig/b": 1, "churnrig-other/c": 1})
        result, _, _ = quiet(rig.clear_bucket, s3, "churnrig", True)
        self.assertEqual(result, (2, 0))
        self.assertEqual(sorted(s3.deleted), ["churnrig/a", "churnrig/b"])
        self.assertIn("churnrig-other/c", s3.objects)


class TeardownCommand(TempDirCase):

    def args(self, *extra):
        return parse("teardown", "--es", "http://x:9200",
                     "--state-file", self.path("s.json"), *extra)

    def write_state(self, **extra):
        state = {"names": rig.names("churnrig"), "base_path": "churnrig",
                 "prior_settings": {rig.ILM_POLL_INTERVAL: "10m"},
                 "settings_changed": {rig.ILM_POLL_INTERVAL: "1m"}}
        state.update(extra)
        pathlib.Path(self.path("s.json")).write_text(json.dumps(state))

    def run_teardown(self, es, args, s3=None):
        return quiet(rig.cmd_teardown, es, args, rig.names("churnrig"), s3,
                     "no s3")

    def test_no_state_file_and_no_flag_refuses_before_deleting(self):
        # Without state every name is a guess at somebody else's repository.
        es = FakeEs()
        with self.assertRaises(SystemExit):
            self.run_teardown(es, self.args())
        self.assertEqual(es.calls, [])

    def test_a_purge_from_a_guessed_path_is_refused_even_when_derived(self):
        # --derive-from-prefix allows deleting cluster objects but never the
        # bucket purge, since bucket objects cannot be rebuilt.
        es = FakeEs()
        with self.assertRaises(SystemExit):
            self.run_teardown(es, self.args("--derive-from-prefix",
                                            "--purge-bucket"), FakeS3())
        self.assertEqual(es.calls, [])

    def test_a_clean_teardown_removes_the_state_file(self):
        # The state file blocks the next run. It must go once nothing is left,
        # and only then.
        self.write_state()
        es = FakeEs()
        code, out, _ = self.run_teardown(es, self.args())
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["clean"])
        self.assertFalse(os.path.exists(self.path("s.json")))

    def test_residue_keeps_the_state_file_and_exits_nonzero(self):
        # The state file is the only record of the original settings. Deleting
        # it while a policy survives would strand them.
        self.write_state()
        es = FakeEs()
        es.persistent = {rig.ILM_POLL_INTERVAL: "1m"}
        es.put = lambda path, body=None, timeout=120: None  # cluster ignores it
        code, out, _ = self.run_teardown(es, self.args())
        self.assertEqual(code, 1)
        self.assertIn("settings_mismatch", json.loads(out))
        self.assertTrue(os.path.exists(self.path("s.json")))

    def test_without_s3_the_verdict_says_the_bucket_was_not_checked(self):
        # A null leftover count must not read as a zero.
        es = FakeEs()
        _, out, err = self.run_teardown(
            es, self.args("--derive-from-prefix"))
        self.assertIsNone(json.loads(out)["leftover_bucket_objects"])
        self.assertIn("bucket state not checked", err)

    def test_an_empty_base_path_on_the_command_line_purges_nothing(self):
        # An unset variable in a wrapper script renders --base-path ''. If
        # that empty value counted as a stated path, the purge would run
        # from the --prefix guess the refusal exists to block, or from the
        # bucket root, and delete another live repository's objects.
        s3 = FakeS3({"churnrig/a": 1, "gcw/index-7": 1})
        es = FakeEs()
        with self.assertRaises(SystemExit):
            self.run_teardown(es, self.args(
                "--derive-from-prefix", "--purge-bucket", "--base-path", ""),
                s3)
        self.assertEqual((es.calls, s3.deleted), ([], []))

    def test_an_empty_base_path_in_the_state_file_purges_nothing(self):
        # The rig never writes an empty base path, so one in the state file
        # was put there by hand or by another tool. Read as written, it
        # scopes the purge to the whole shared bucket, and every tenant's
        # objects go with no way back.
        for empty in ("", "/", None):
            with self.subTest(base_path=empty):
                self.write_state(base_path=empty)
                s3 = FakeS3({"churnrig/a": 1, "gcw/index-7": 1})
                es = FakeEs()
                with self.assertRaises(SystemExit):
                    self.run_teardown(es, self.args("--purge-bucket"), s3)
                self.assertEqual((es.calls, s3.deleted), ([], []))

    def test_a_state_file_without_a_base_path_does_not_license_a_purge(self):
        # A state file that never recorded a base path states no scope.
        # Filling the gap from --prefix would purge a guessed path, which is
        # the one thing purge_refusal says must stay refused.
        self.write_state(base_path=None)
        state = json.loads(pathlib.Path(self.path("s.json")).read_text())
        del state["base_path"]
        pathlib.Path(self.path("s.json")).write_text(json.dumps(state))
        s3 = FakeS3({"churnrig/a": 1})
        es = FakeEs()
        with self.assertRaises(SystemExit):
            self.run_teardown(es, self.args("--purge-bucket"), s3)
        self.assertEqual((es.calls, s3.deleted), ([], []))

    def test_an_empty_base_path_does_not_block_a_teardown_that_skips_the_bucket(
            self):
        # With no S3 client and no purge the bucket is never listed, so the
        # empty value scopes nothing. Refusing here would leave the rig's
        # SLM policy writing snapshots and its cluster settings changed.
        self.write_state(base_path="")
        es = FakeEs()
        code, _, _ = self.run_teardown(es, self.args())
        self.assertEqual(code, 0)
        self.assertIn(rig.SLM_POLICY_PATH + "churnrig-slm", es.paths("DELETE"))

    def test_objects_a_purge_left_behind_keep_the_state_file(self):
        # A store can acknowledge a single delete and keep the object. If
        # teardown still called that clean, it would delete the state file,
        # and finishing the purge would then need --base-path typed from
        # memory in a bucket shared with live repositories.
        class DeletesThatDoNotTake(FakeS3):
            def delete_object(self, key):
                self.deleted.append(key)

        self.write_state()
        s3 = DeletesThatDoNotTake({"churnrig/a": 1, "churnrig/b": 1})
        code, out, _ = self.run_teardown(
            FakeEs(), self.args("--purge-bucket"), s3)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["clean"])
        self.assertTrue(os.path.exists(self.path("s.json")))

    def test_purge_reports_what_it_removed(self):
        # The verdict is the audit trail of an irreversible delete.
        self.write_state()
        s3 = FakeS3({"churnrig/a": 1})
        _, out, _ = self.run_teardown(
            FakeEs(), self.args("--purge-bucket"), s3)
        verdict = json.loads(out)
        self.assertEqual((verdict["purged"],
                          verdict["leftover_bucket_objects"]), (1, 0))


class RemoveStateFile(TempDirCase):

    def test_a_state_path_that_resolves_to_a_directory_is_not_removed(self):
        # The run is over and its verdict printed, so this reports and moves
        # on. Removing something that is not the checked file would delete
        # data the rig never wrote.
        os.mkdir(self.path("d"))
        _, _, err = quiet(rig.remove_state_file, self.path("d"))
        self.assertIn("nothing was removed", err)
        self.assertTrue(os.path.isdir(self.path("d")))

    def test_a_symlink_is_resolved_and_its_target_is_what_goes(self):
        # The checked file and the removed file must be the same one.
        target = self.path("real.json")
        pathlib.Path(target).write_text("{}")
        os.symlink(target, self.path("link.json"))
        quiet(rig.remove_state_file, self.path("link.json"))
        self.assertFalse(os.path.exists(target))

    def test_an_unremovable_file_is_a_warning_not_a_failure(self):
        # A verified clean teardown must still exit 0. The next run refuses
        # to start while the file exists, so the warning says to delete it.
        pathlib.Path(self.path("s.json")).write_text("{}")
        with mock.patch.object(rig.os, "unlink",
                               side_effect=PermissionError("no")):
            _, _, err = quiet(rig.remove_state_file, self.path("s.json"))
        self.assertIn("delete it", err)


class StatusCommand(TempDirCase):

    def test_status_prefers_the_names_the_state_file_recorded(self):
        # A run that chose its own names must be reported under them. Using
        # the prefix-derived names would show an empty rig that is not.
        names = rig.names("other")
        pathlib.Path(self.path("s.json")).write_text(json.dumps(
            {"names": names, "base_path": "bp"}))
        es = FakeEs()
        es.present[rig.SNAPSHOTS_IN_REPO_PATH % "other-repo"] = {
            "snapshots": [{"snapshot": "other-snap-1", "uuid": "u",
                           "state": "SUCCESS", "start_time_in_millis": 0}]}
        args = parse("status", "--es", "http://x:9200",
                     "--state-file", self.path("s.json"))
        code, out, _ = quiet(rig.cmd_status, es, args,
                             rig.names("churnrig"), None, "no s3")
        self.assertEqual(json.loads(out)["snapshots"]["alive"], 1)

    def test_status_refuses_an_empty_base_path_instead_of_listing_the_bucket(
            self):
        # Setup maps an empty --base-path to the prefix, so status reading
        # the same value as the bucket root reports another repository's
        # generations as this rig's and sends the operator after a leak
        # that is not there.
        args = parse("status", "--es", "http://x:9200", "--base-path", "",
                     "--state-file", self.path("none.json"))
        with self.assertRaises(SystemExit):
            quiet(rig.cmd_status, FakeEs(), args, rig.names("churnrig"),
                  FakeS3({"gcw/index-7": 1}), None)

    def test_status_before_setup_falls_back_to_the_prefix(self):
        # status is useful before setup has run, which the doc promises.
        args = parse("status", "--es", "http://x:9200",
                     "--state-file", self.path("none.json"))
        code, out, _ = quiet(rig.cmd_status, FakeEs(), args,
                             rig.names("churnrig"), None, "no s3")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["prefix"], "churnrig")


if __name__ == "__main__":
    unittest.main()
