#!/usr/bin/env python3
"""Turn the JSON the scan step saved into the summary a person reads.

Written as markdown to stdout. The build step sends it to the run summary and
to a single pull request comment that gets edited in place, so a busy branch
does not collect one comment per push.

Reads only files already on disk. It never calls the API, so it cannot fail
the build over a network hiccup after the analysis already succeeded.
"""

import collections
import json
import os

RATINGS = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}
RATED = ("security_rating", "reliability_rating", "sqale_rating",
         "security_review_rating")
NAMES = {
    "security_rating": "Security",
    "reliability_rating": "Reliability",
    "sqale_rating": "Maintainability",
    "security_review_rating": "Security review",
}


def load(name):
    try:
        with open(os.path.join("sonar-export", name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def measures():
    got = {}
    for entry in load("measures.json").get("component", {}).get("measures", []):
        value = entry.get("value")
        if value is None:
            period = entry.get("period") or {}
            value = period.get("value")
        got[entry["metric"]] = value
    return got


def main():
    got = measures()
    gate = load("gate.json").get("projectStatus", {})
    issues = load("issues.json").get("issues", [])

    status = gate.get("status", "UNKNOWN")
    mark = {"OK": "passed", "ERROR": "failed"}.get(status, status.lower())
    print(f"## Scan results: quality gate {mark}\n")

    if any(m in got for m in RATED):
        print("| Rating | |")
        print("|---|---|")
        for metric in RATED:
            if metric in got:
                letter = RATINGS.get(str(got[metric]).split(".")[0], got[metric])
                print(f"| {NAMES[metric]} | **{letter}** |")
        print()

    counts = [("Bugs", "bugs"), ("Vulnerabilities", "vulnerabilities"),
              ("Code smells", "code_smells"),
              ("Security hotspots", "security_hotspots"),
              ("Coverage", "coverage"), ("Lines", "ncloc")]
    shown = [(label, got[key]) for label, key in counts if key in got]
    if shown:
        print("| Measure | Value |")
        print("|---|---|")
        for label, value in shown:
            suffix = "%" if label == "Coverage" else ""
            print(f"| {label} | {value}{suffix} |")
        print()

    failing = [c for c in gate.get("conditions", []) if c.get("status") != "OK"]
    if failing:
        print("### Conditions not met\n")
        print("| Condition | Actual | Required |")
        print("|---|---|---|")
        for c in failing:
            print(f"| `{c['metricKey']}` | {c.get('actualValue')} | "
                  f"{c.get('comparator', '')} {c.get('errorThreshold')} |")
        print()

    if issues:
        print(f"### Open issues ({len(issues)})\n")
        by_rule = collections.Counter(
            (i.get("severity", "?"), i.get("rule", "?")) for i in issues)
        print("| Count | Severity | Rule |")
        print("|---|---|---|")
        for (severity, rule), n in by_rule.most_common():
            print(f"| {n} | {severity} | `{rule}` |")
        print()
        print("<details><summary>Every issue, by location</summary>\n")
        for i in issues:
            where = i.get("component", "").split(":", 1)[-1]
            print(f"- `{where}:{i.get('line', '?')}` "
                  f"{i.get('severity', '?')} `{i.get('rule', '?')}` "
                  f"{i.get('message', '')}")
        print("\n</details>")
    else:
        print("No open issues.")


if __name__ == "__main__":
    main()
