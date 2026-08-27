"""An optional Elasticsearch veto. It protects. It never condemns.

Two facts about a repository live in cluster state and appear nowhere in the
bucket, and both of them cost data when they are missed:

  * WHICH SNAPSHOTS HAVE SEARCHABLE-SNAPSHOT INDICES MOUNTED ON THEM.
    Elasticsearch does not block deleting a snapshot that backs a mounted
    index, and SLM has no mount awareness at all, so a retention policy reaps
    one on schedule and the mounted index fails at its next restart with
    nothing connecting the failure to the sweep. This is the single most
    valuable thing a cluster can say about a manifest.
  * WHICH SNAPSHOTS ARE IN FLIGHT RIGHT NOW.

The veto is ONE DIRECTIONAL. Everything Elasticsearch reports is REMOVED from
the manifest. Nothing Elasticsearch fails to report is thereby condemnable:
absence in its answer means nothing, the same way absence means nothing
everywhere else in this package. That is what keeps this from becoming a
second source of condemnation, and it is why the veto is subtracted from a
finished manifest rather than consulted while deciding what to condemn. It
cannot make the output larger by construction rather than by test.

WHAT IT DOES NOT DO, AND AN OPERATOR WILL OVERESTIMATE THIS. The veto
protects by SNAPSHOT IDENTITY: it matches a manifest row against the uuid that
row was attributed to. So it cannot catch a failure of ATTRIBUTION. If a
misread makes a live snapshot's blob look like it belonged to a dead one, the
row wears the dead snapshot's uuid, the cluster is not protecting that uuid,
and the veto passes the row through. A reviewer confirmed that against a
realistic cluster answer. Corroboration covers "this snapshot is still in use"
and covers nothing about whether the derivation put the right key under the
right snapshot. The guards in `derivation/shards.py` are what stand there.

It does not close issue #1. Snapshot EXISTENCE is
derived by Elasticsearch from the same bucket this tool reads, so a consistent
tamper defeats both. The MOUNT information and the IN-FLIGHT information are
genuinely independent of the bucket; the snapshot list is not. This claims the
first and not the second.

AN UNOBTAINED VETO IS NOT AN EMPTY ONE. There is no code path here that turns
a failed call into an empty set of protections. A caller who asked for
corroboration and could not have it gets an exception, and the run refuses.
Proceeding would produce a LARGER manifest than a successful call would have,
which is the one property this tool exists to guarantee it never does. Every
failure in this module raises, and this project's own test suite holds a
static tripwire that fails if any `except` here ever learns to return.

IF THIS FAILS, FIX THE ACCESS, NOT THE FLAG. A 403, an unreachable endpoint or
an unparseable answer means the run cannot be corroborated, and the remedy is
the credential, the endpoint or the network. Dropping the flag is not a remedy
and this module's documentation will not offer it as one. That is not
pedantry: this project's own retired tools had this guard right in code and
lost it to prose that told an operator the flag was optional after handing
them a key that 403s on the very call the guard needs.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from .credentials import as_secret
from .errors import GenerationChainError
from .model import Condemnation

SNAPSHOT_SETTINGS_PREFIX = "index.store.snapshot."
DEFAULT_TIMEOUT_SECONDS = 60.0
USER_AGENT = "generation-chain-auditor (python-urllib)"


class CorroborationUnavailable(GenerationChainError):
    """Corroboration was asked for and could not be obtained.

    `transient` separates a cluster that was busy from a credential that is
    wrong, because a scheduled job should retry the first and not the second.
    """

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class Credentials:
    """Read from the environment by the caller. Never from argv."""

    username: Optional[str] = None
    password: Optional[Any] = None
    api_key: Optional[Any] = None

    def __post_init__(self) -> None:
        for field in ("password", "api_key"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, as_secret(value))

    def header(self) -> Dict[str, str]:
        if self.api_key:
            return {"Authorization": f"ApiKey {self.api_key.reveal()}"}
        if self.username is not None and self.password is not None:
            pair = f"{self.username}:{self.password.reveal()}".encode("utf-8")
            return {"Authorization":
                    "Basic " + base64.b64encode(pair).decode("ascii")}
        return {}


@dataclass(frozen=True)
class Veto:
    """What Elasticsearch says must not be touched.

    Constructed only from an answer that arrived and parsed. There is
    deliberately no default, no empty singleton and no factory that builds one
    from nothing, so "I could not ask" has no way to spell itself as "there is
    nothing to protect".
    """

    endpoint: str
    snapshot_uuids: FrozenSet[str]
    index_uuids: FrozenSet[str]
    mounted_indices: Tuple[str, ...]
    in_flight: Tuple[str, ...]
    snapshots_reported: int

    def covers(self, condemnation: Condemnation) -> bool:
        """Whether Elasticsearch protects this key."""
        if condemnation.snapshot_uuid in self.snapshot_uuids:
            return True
        for index_uuid in self.index_uuids:
            if condemnation.key.startswith(f"indices/{index_uuid}/"):
                return True
        return False

    def apply(self, condemned):
        """Subtract. This is the only way a Veto reaches the manifest."""
        return [row for row in condemned if not self.covers(row)]


class ElasticsearchVeto:
    """Asks one cluster about one repository, or raises."""

    def __init__(self, endpoint: str, repository: str,
                 credentials: Credentials,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 ca_certificate: Optional[str] = None,
                 opener=None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.repository = repository
        self.credentials = credentials
        self.timeout = timeout
        self._context = (ssl.create_default_context(cafile=ca_certificate)
                         if ca_certificate else None)
        self._opener = opener or urllib.request.urlopen

    def fetch(self) -> Veto:
        """The protections, or CorroborationUnavailable. Never an empty veto."""
        snapshots = self._get(
            f"/_snapshot/{urllib.parse.quote(self.repository, safe='')}/_all")
        mounted = self._get(
            "/_all/_settings/index.store.snapshot.*"
            "?expand_wildcards=all&flat_settings=true&ignore_unavailable=true")
        running = self._get("/_snapshot/_status")
        return _build_veto(self.endpoint, snapshots, mounted, running)

    def _get(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(self.endpoint + path, method="GET")
        for name, value in self.credentials.header().items():
            request.add_header(name, value)
        request.add_header("User-Agent", USER_AGENT)
        try:
            with self._opener(request, timeout=self.timeout,
                              **({"context": self._context}
                                 if self._context else {})) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise CorroborationUnavailable(
                f"Elasticsearch answered {exc.code} for {path}. Corroboration "
                "was asked for and could not be obtained, so this run "
                "explains nothing. Check the credential's privileges, the "
                "endpoint and the repository name",
                transient=exc.code >= 500 or exc.code == 429) from exc
        except Exception as exc:
            raise CorroborationUnavailable(
                f"cannot reach Elasticsearch for {path}: "
                f"{type(exc).__name__}: {exc}", transient=True) from exc
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorroborationUnavailable(
                f"Elasticsearch's answer for {path} is not JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise CorroborationUnavailable(
                f"Elasticsearch's answer for {path} is not an object")
        return document


def _build_veto(endpoint: str, snapshots: Mapping[str, Any],
                mounted: Mapping[str, Any],
                running: Mapping[str, Any]) -> Veto:
    """Assemble the protections from three answers that all arrived."""
    protected_snapshots = set()
    reported = snapshots.get("snapshots")
    if not isinstance(reported, list):
        raise CorroborationUnavailable(
            "Elasticsearch's snapshot list carries no snapshots array")
    for entry in reported:
        if not isinstance(entry, dict) or not isinstance(entry.get("uuid"), str):
            raise CorroborationUnavailable(
                "Elasticsearch reported a snapshot with no uuid")
        protected_snapshots.add(entry["uuid"])

    protected_indices = set()
    mounted_names = []
    for name, body in mounted.items():
        settings = body.get("settings") if isinstance(body, dict) else None
        if not isinstance(settings, dict):
            continue
        snapshot_uuid = settings.get(SNAPSHOT_SETTINGS_PREFIX + "snapshot_uuid")
        index_uuid = settings.get(SNAPSHOT_SETTINGS_PREFIX + "index_uuid")
        if not isinstance(snapshot_uuid, str):
            continue
        mounted_names.append(str(name))
        protected_snapshots.add(snapshot_uuid)
        if isinstance(index_uuid, str):
            protected_indices.add(index_uuid)

    in_flight = []
    running_snapshots = running.get("snapshots")
    if not isinstance(running_snapshots, list):
        # An answer this tool cannot read protects nothing, and protecting
        # nothing makes the manifest LONGER than a readable answer would have.
        # That is the whole failure mode this module exists to make impossible,
        # so an unreadable answer is an unobtained one.
        raise CorroborationUnavailable(
            "Elasticsearch's in-flight snapshot status carries no snapshots "
            "array, so this run could not establish what is being written now")
    for entry in running_snapshots:
        if not isinstance(entry, dict) or not isinstance(entry.get("uuid"), str):
            raise CorroborationUnavailable(
                "Elasticsearch reported an in-flight snapshot with no uuid")
        protected_snapshots.add(entry["uuid"])
        in_flight.append(str(entry.get("snapshot", entry["uuid"])))

    return Veto(endpoint=endpoint,
                snapshot_uuids=frozenset(protected_snapshots),
                index_uuids=frozenset(protected_indices),
                mounted_indices=tuple(sorted(mounted_names)),
                in_flight=tuple(sorted(in_flight)),
                snapshots_reported=len(reported))
