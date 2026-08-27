#!/usr/bin/env python3
"""Elasticsearch calls for the campaign, through kubectl exec because the rig
speaks plain HTTP on 127.0.0.1:9200 inside the pod only."""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PW = open(os.path.join(ROOT, "env", "es_pass")).read().strip()
KUBECTL = ["kubectl", "--context", "rancher-desktop", "-n", "es-rig"]
POD = ["exec", "-i", "rig-es-default-0", "-c", "elasticsearch", "--"]


def call(method, path, body=None, timeout=600):
    cmd = KUBECTL + POD + [
        "curl", "-s", "-X", method, "-u", "elastic:" + PW,
        "-H", "Content-Type: application/json",
    ]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    cmd += ["http://127.0.0.1:9200" + path]
    r = subprocess.run(
        cmd,
        input=(json.dumps(body) if body is not None else None),
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout


def jcall(method, path, body=None, timeout=600):
    raw = call(method, path, body, timeout)
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}
