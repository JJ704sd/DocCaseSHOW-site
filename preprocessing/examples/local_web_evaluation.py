"""Project the reviewed regression artifacts for the local workbench."""

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "output/evaluation"
FIXED_RUN = "field-review"
TIMING_RUN = "email-timing"
HITS = ("主值命中", "补充命中", "货行命中")
GROUP_COLUMNS = ("J", "K", "L", "M", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z")
ROW_COLUMNS = ("AD", "AE", "AF", "AG", "AH", "AI", "AJ", "AK", "AM")


def _project_fields(rows):
    columns = {}
    totals = Counter()
    for row in rows:
        if row["diagnostic_only"]:
            continue
        label, counts = columns.setdefault(row["column"], (row["field"], Counter()))
        if label != row["field"]:
            raise ValueError("Inconsistent evaluation column")
        counts[row["after"]] += 1
        totals[row["after"]] += 1
    if columns.keys() != set((*GROUP_COLUMNS, *ROW_COLUMNS)):
        raise ValueError("Incomplete 23-field evaluation")
    results = []
    for scope, names in (("group", GROUP_COLUMNS), ("row", ROW_COLUMNS)):
        for name in names:
            label, counts = columns[name]
            results.append(
                {
                    "scope": scope,
                    "column": name,
                    "label": label,
                    "reviewed": counts.total() - counts["无非空精标"],
                    "hits": sum(counts[key] for key in HITS),
                }
            )
    return results, totals


def saved_evaluation(root=ROOT):
    def read(run, file):
        return json.loads((root / run / file).read_bytes())

    verified = read(FIXED_RUN, "verification.json")
    fields, totals = _project_fields(read(FIXED_RUN, "field-comparison.json"))
    if totals != Counter(verified["after"]):
        raise ValueError("Evaluation counts differ from verification")
    past = verified["before"]
    latency = read(TIMING_RUN, "latency-comparison.json")["wall_s"]["stream"]
    cases = []
    return {
        "run": FIXED_RUN,
        "cohort": {
            "groups": verified["groups"],
            "emails": len(read(TIMING_RUN, "intake.json")),
            "reads": verified["fixed_reads"],
            "fields": len(fields),
        },
        "confirmed": {
            "before": sum(past.get(k, 0) for k in HITS),
            "hits": sum(totals[k] for k in HITS),
            "wrong": totals["误填"],
            "primary": totals[HITS[0]],
            "supplements": totals[HITS[1]],
            "cargo_rows": totals[HITS[2]],
            "still_missing_primary": verified["still_missing_primary"],
            "reviewed": sum(f["reviewed"] for f in fields),
            "missing_original": totals["未见本轮目标格式原件"],
        },
        "timing": {
            "historical_run": TIMING_RUN,
            "seconds_per_email": {k: latency[k] for k in ("mean", "p50", "p95")},
            "replay_batch_seconds": verified["elapsed_seconds"],
        },
        "fields": fields,
        "cases": cases,
    }
