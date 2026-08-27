"""Shared wiring: where the tool is, how a run is measured, how it is reported."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
import tracemalloc

sys.dont_write_bytecode = True  # never leave a __pycache__ in someone's worktree

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.dirname(HERE)
DEFAULT_TOOL_ROOT = os.path.join(SCRATCH, "tool-snapshot")
RESULTS = os.path.join(SCRATCH, "results")


def use_tool(root: str = None) -> str:
    root = root or os.environ.get("GENCHAIN_TOOL_ROOT") or DEFAULT_TOOL_ROOT
    root = os.path.abspath(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    return root


def peak_rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def rss_kb() -> int:
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.t0
        return False


def emit(name: str, rows) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name + ".json")
    with open(path, "w") as handle:
        json.dump(rows, handle, indent=1, sort_keys=True)
    return path


def table(rows, columns) -> str:
    widths = [max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows))
              for c in columns] if rows else [len(str(c)) for c in columns]
    out = [" | ".join(str(c).ljust(w) for c, w in zip(columns, widths)),
           "-|-".join("-" * w for w in widths)]
    for row in rows:
        out.append(" | ".join(str(row.get(c, "")).ljust(w)
                              for c, w in zip(columns, widths)))
    return "\n".join(out)


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--tool-root", default=None,
                        help="directory holding the generation_chain package "
                             "to measure (default: the frozen snapshot)")
    return parser
