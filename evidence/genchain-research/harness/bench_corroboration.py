"""What --elasticsearch adds to the list of round trips that can end a run.

The store reads get eight attempts and a jittered backoff. The cluster calls
go through `urllib.request.urlopen` directly, with no retry policy at all, and
the tool refuses the run if any of them does not answer. That is a deliberate
choice, stated in the code: proceeding without the veto would produce a LONGER
manifest than a successful call would. It is also three more chances per run
to lose the whole run, and unlike the store reads they are not retried.

This counts the calls and confirms what one failure does to each.
"""
from __future__ import annotations
import json, os, sys, urllib.error

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool
parser = base_parser(__doc__)
parser.add_argument("--out", default="corroboration")
args = parser.parse_args()
use_tool(args.tool_root)

from generation_chain.corroboration import (CorroborationUnavailable,
                                            ElasticsearchVeto)
from generation_chain.corroboration import Credentials


class Fake:
    """Answers the cluster calls, and fails one chosen path once."""

    def __init__(self, fail_path=None, status=503):
        self.paths = []
        self.fail_path = fail_path
        self.status = status
        self.attempts = 0

    def __call__(self, request, timeout=None, context=None):
        url = request.full_url
        path = url.split("/", 3)[-1]
        self.paths.append("/" + path)
        self.attempts += 1
        if self.fail_path and ("/" + path).startswith(self.fail_path):
            raise urllib.error.HTTPError(url, self.status, "fault", {}, None)
        if "_status" in path:
            body = {"snapshots": []}
        elif "_all" in path:
            body = {"snapshots": [{"uuid": "u1", "snapshot": "s1"}]}
        else:
            body = {}
        return _Body(json.dumps(body).encode())


class _Body:
    def __init__(self, data):
        self._data = data
        self.status = 200
        self.headers = {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


credentials = Credentials(username="u", password="p") \
    if "username" in Credentials.__dataclass_fields__ else None
if credentials is None:
    raise SystemExit("credential shape changed; adjust this bench")

probe = Fake()
ElasticsearchVeto(endpoint="https://es:9200", repository="r",
                  credentials=credentials, opener=probe).fetch()
calls = list(probe.paths)
rows = [{"call": p, "attempts_when_it_fails": None, "outcome": "answered"}
        for p in calls]

for path in calls:
    fake = Fake(fail_path=path.split("?")[0].rstrip("/"))
    try:
        ElasticsearchVeto(endpoint="https://es:9200", repository="r",
                          credentials=credentials, opener=fake).fetch()
        outcome = "run continued"
    except CorroborationUnavailable as exc:
        outcome = ("run refused, transient" if exc.transient
                   else "run refused, settled")
    for row in rows:
        if row["call"] == path:
            row["outcome"] = outcome
            row["attempts_when_it_fails"] = sum(
                1 for p in fake.paths if p == path)

summary = {"cluster_calls_per_run": len(calls),
           "retry_attempts_each": max(r["attempts_when_it_fails"] or 0
                                      for r in rows),
           "fatal_calls": sum(1 for r in rows if "refused" in r["outcome"])}
print(table(rows, ["call", "attempts_when_it_fails", "outcome"]))
print(json.dumps(summary))
print(emit(args.out, {"calls": rows, "summary": summary}))
