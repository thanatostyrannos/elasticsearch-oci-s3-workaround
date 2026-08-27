"""Checks that must hold at the moment of deletion, not when the list was made.

The audit decides what is unreferenced and writes a manifest. Deleting happens
later, as a separate act, by a person who approved that exact file. Between
those two moments the cluster keeps running, and one thing it can do in that
window matters: mount a searchable snapshot over blobs the manifest already
names.

Elasticsearch does not stop that. It will let a snapshot backing a mounted
index be deleted, and SLM has no mount awareness at all, so retention will reap
one on schedule. The mounted index stays green until something forces it to
read, and fails later with nothing connecting the failure to the sweep.

The veto exists for exactly that case, and it ran only when the manifest was
derived. `reclaim/` referenced Elasticsearch nowhere, and nothing read the
manifest's age, so a list written yesterday could be executed today against a
cluster that had changed underneath it.

This module is that gap closed. It is a TIME-OF-CHECK problem, not the absence
test the safety model is usually argued about: the set difference behind the
manifest is Elasticsearch's own and shard-local, and a subset of what
Elasticsearch would collect itself.

Everything here is a pure function over values, so the decisions can be tested
without a cluster.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

# An hour. Long enough to read a manifest, look at it, and decide; short enough
# that the cluster is unlikely to have gained a mount since. Not a measurement,
# a judgement, and the operator can change it.
DEFAULT_MAX_MANIFEST_AGE_SECONDS = 3600


def staleness_problem(age_seconds: float, maximum: float,
                      path: str) -> Optional[str]:
    """Why this manifest is too old to act on, or None.

    A maximum of zero disables the check. That has to be possible, because an
    operator working a long incident may have decided for themselves, and it
    has to be an explicit act rather than the default, because the failure it
    guards against is silent.
    """
    if maximum <= 0:
        return None
    if age_seconds <= maximum:
        return None
    return (f"{path} was written {int(age_seconds)}s ago and the limit is "
            f"{int(maximum)}s. The cluster can gain a mounted searchable "
            "snapshot over these blobs in that time, and the protection was "
            "checked when the manifest was derived, not now. Derive it again. "
            "To act on it anyway, say so with --max-manifest-age 0.")


def newly_protected(keys: Iterable[str], veto) -> Tuple[str, ...]:
    """Manifest keys the cluster now protects, in file order.

    Matched on the `indices/<index-uuid>/` prefix, because a veto's
    `index_uuids` are populated ONLY from mounted searchable snapshots. That is
    the half of the protection which can change between deriving a manifest and
    executing it, and it is the half worth re-reading.

    Anchored on the trailing slash. `indices/AAAA` must not protect
    `indices/AAAABBBB`.
    """
    prefixes = tuple(f"indices/{uuid}/" for uuid in veto.index_uuids)
    if not prefixes:
        return ()
    return tuple(key for key in keys if key.startswith(prefixes))


def corroboration_choice_problem(elasticsearch: Optional[str],
                                 without: bool) -> Optional[str]:
    """Why this run has not said whether to re-check, or None.

    Deleting requires the operator to choose, rather than inheriting a default
    either way. Defaulting to checking would break anyone reclaiming an
    orphaned repository with no cluster left to ask, and push them toward a
    worse tool. Defaulting to not checking would make the dangerous path the
    quiet one.
    """
    if elasticsearch and without:
        return ("--elasticsearch and --without-elasticsearch contradict each "
                "other. Pick the one you mean.")
    if not elasticsearch and not without:
        return ("--execute needs to know whether to re-check the "
                "Elasticsearch veto against the cluster as it is NOW. The "
                "manifest's protection was decided when it was derived, and a "
                "searchable snapshot mounted since then would not be in it. "
                "Pass --elasticsearch with --es-repository to re-check, or "
                "--without-elasticsearch to state that no cluster can be "
                "asked, which is the case when the repository is orphaned.")
    return None


def protection_problem(protected: Sequence[str], total: int) -> Optional[str]:
    """Why a re-checked manifest must not be executed, or None.

    Refuses the whole run rather than quietly deleting the rest. The approval
    binds this exact file: an operator who approved a digest over N rows did
    not approve N minus however many the cluster started protecting while they
    were reading it.
    """
    if not protected:
        return None
    shown = "\n".join(f"  {key}" for key in protected[:10])
    more = ("\n  ... and %d more" % (len(protected) - 10)
            if len(protected) > 10 else "")
    return (f"{len(protected)} of {total} key(s) in this manifest are now "
            "protected by the cluster, which they were not when it was "
            f"derived. A searchable snapshot has been mounted over them since.\n"
            f"{shown}{more}\n"
            "Nothing was deleted. Derive the manifest again against the "
            "cluster as it is now.")
