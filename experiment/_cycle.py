"""What scripts/run-test-cycle.sh does, without needing a shell.

The shell version reads a config file, checks the things that otherwise fail
confusingly on cycle forty, and then runs the harness. It needs bash, and it
shells out to curl to see whether the cluster answers.

This needs neither. It is the same preflight in the language everything else
here is written in, which means the single-file build has no reason to write
anything to disk: `cycle` runs the loop rather than handing you a script that
runs the loop.

The config format is unchanged, so a file written for the shell version works
here. It is read as KEY="value" lines rather than sourced, because sourcing a
config file executes it, and a config file that can run commands is not a
config file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

REQUIRED = ("ENDPOINT", "REGION", "BUCKET", "PREFIX", "CREDENTIALS",
            "REPOSITORY", "OUT")
DEFAULTS = {"CYCLES": "100", "MODE": "mixed", "SLEEP_BETWEEN": "5",
            "SETTLE_TIMEOUT": "300", "DRY_RUN_ONLY": "yes"}
# The line is stripped before this runs, so anchoring whitespace on both ends
# and making the value lazy between two optional quotes bought nothing and let
# the value be matched several ways. Take the rest of the line as it comes and
# deal with the quotes afterwards, which is one pass instead of a search.
LINE = re.compile(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$')


class Refused(Exception):
    """A preflight check said no, with a reason worth printing."""


def _unquote(value):
    """Drop one pair of surrounding double quotes, if that is what they are."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def read_config(path):
    """Parse KEY="value" lines. Never execute the file."""
    resolved = require_private(path, "the config file")
    values = dict(DEFAULTS)
    for number, raw in enumerate(open(resolved, encoding="utf-8"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if not match:
            raise Refused(f"{path}:{number} is not KEY=value: {line[:60]}")
        values[match.group(1)] = _unquote(match.group(2))
    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        raise Refused(f"{path} does not set: {', '.join(missing)}")
    return values


def require_private(path, what):
    """Refuse a file other users can read, the way the tools themselves do.

    Resolves symlinks first and refuses anything that is not an ordinary
    file, so a config value can't point at a device, a pipe, or a directory
    dressed up as a file path. Returns the resolved path; callers open that
    one, not the one they were given, so the path that got checked is the
    path that gets read.
    """
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        raise Refused(f"no such file: {path}")
    mode = stat.S_IMODE(os.stat(resolved).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refused(
            f"{what} {path} is mode {mode:04o} and must be 0600 or 0400. "
            "Any other mode lets other users on this host read it")
    return resolved


def check_credentials(path, wants_cluster):
    resolved = require_private(path, "the credentials file")
    try:
        document = json.load(open(resolved, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{path} is not readable JSON: {exc}") from None
    if "s3" not in document:
        raise Refused(f"{path} has no 's3' section")
    if wants_cluster:
        section = document.get("elasticsearch") or {}
        ok = "api_key" in section or ("username" in section and "password" in section)
        if not ok:
            raise Refused(
                f"{path} needs an 'elasticsearch' section with an api_key, or "
                "a username and password. The audit reads its cluster "
                "credential from there and takes nothing from this harness")


def check_endpoint(endpoint):
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme == "https":
        return
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme == "http" and loopback:
        return
    raise Refused(
        f"ENDPOINT is {endpoint}. A manifest names which objects are about to "
        "be deleted, so plain http off loopback is refused. Use https, or "
        "pass --insecure-http yourself if this really is a lab store")


def check_cluster(url, password_file):
    """Ask the cluster whether it answers. No curl, no shell."""
    resolved = require_private(password_file, "the password file")
    password = open(resolved, encoding="utf-8").read().strip()
    request = urllib.request.Request(url.rstrip("/") + "/_cluster/health")
    import base64
    token = base64.b64encode(f"elastic:{password}".encode()).decode()
    request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise Refused(f"{url} answered HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise Refused(
            f"{url} answered HTTP {exc.code}. If that is 401 the password is "
            "stale: it is regenerated whenever the cluster is rebuilt") from None
    except Exception as exc:                              # noqa: BLE001
        raise Refused(f"cannot reach {url}: {exc}") from None


def harness_argv(config):
    argv = ["--cycles", config["CYCLES"], "--mode", config["MODE"],
            "--sleep", config["SLEEP_BETWEEN"],
            "--settle-timeout", config["SETTLE_TIMEOUT"],
            "--transport", "s3", "--endpoint", config["ENDPOINT"],
            "--region", config["REGION"], "--bucket", config["BUCKET"],
            "--prefix", config["PREFIX"], "--credentials", config["CREDENTIALS"],
            "--repository", config["REPOSITORY"], "--out", config["OUT"]]
    if config.get("DATA_STREAM"):
        argv += ["--data-stream", config["DATA_STREAM"]]
    if config.get("ELASTICSEARCH"):
        argv += ["--elasticsearch", config["ELASTICSEARCH"],
                 "--es-password-file", config["ES_PASSWORD_FILE"]]
    if config.get("DRY_RUN_ONLY", "yes") == "yes":
        argv.append("--dry-run-only")
    return argv


def tally(out_dir):
    """Totals from the per-cycle execute files, not from the summary line.

    The summary has been wrong before. The execute files are what happened.
    """
    totals = {"deleted": 0, "failed": 0, "unconfirmed": 0}
    pattern = re.compile(r"^(deleted|failed|unconfirmed):\s+(\d+)", re.M)
    if not os.path.isdir(out_dir):
        return totals, 0
    files = [f for f in sorted(os.listdir(out_dir)) if f.startswith("exec-")]
    for name in files:
        try:
            text = open(os.path.join(out_dir, name), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        for key, value in pattern.findall(text):
            totals[key] += int(value)
    return totals, len(files)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the audit-and-reclaim loop from a config file, with "
                    "the preflight checks that turn a confusing failure on "
                    "cycle forty into a clear one before cycle one.")
    parser.add_argument("config", help="a config file, mode 0600")
    args = parser.parse_args(argv)

    try:
        config = read_config(args.config)
        check_endpoint(config["ENDPOINT"])
        check_credentials(config["CREDENTIALS"], bool(config.get("ELASTICSEARCH")))
        if config.get("ELASTICSEARCH"):
            if not config.get("ES_PASSWORD_FILE"):
                raise Refused("ELASTICSEARCH is set but ES_PASSWORD_FILE is not")
            check_cluster(config["ELASTICSEARCH"], config["ES_PASSWORD_FILE"])
            sys.stderr.write("  cluster answers, corroboration is available\n")
        else:
            sys.stderr.write("  no ELASTICSEARCH set: running without corroboration\n")
    except Refused as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    os.makedirs(config["OUT"], exist_ok=True)
    if config.get("DRY_RUN_ONLY", "yes") == "yes":
        sys.stderr.write("  DRY RUN ONLY. Nothing will be deleted.\n")
    else:
        sys.stderr.write(f"  DELETES ARE ENABLED against "
                         f"{config['BUCKET']}/{config['PREFIX']}\n")

    import reclaim_test_protocol
    saved = sys.argv
    sys.argv = ["cycle"] + harness_argv(config)
    try:
        status = reclaim_test_protocol.main()
    finally:
        sys.argv = saved

    totals, count = tally(config["OUT"])
    sys.stderr.write(
        f"  cycles recorded: {count}\n"
        f"  deleted={totals['deleted']} failed={totals['failed']} "
        f"unconfirmed={totals['unconfirmed']}\n")
    return 0 if status is None else status


if __name__ == "__main__":
    raise SystemExit(main())
