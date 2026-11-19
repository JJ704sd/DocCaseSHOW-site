"""Only aggregate local evaluation data is exposed to the workbench."""

import json

import local_web_evaluation as evaluation


def test_saved_evaluation_returns_aggregates_without_case_details(tmp_path):
    current = tmp_path / evaluation.FIXED_RUN
    timing = tmp_path / evaluation.TIMING_RUN
    current.mkdir()
    timing.mkdir()
    columns = (*evaluation.GROUP_COLUMNS, *evaluation.ROW_COLUMNS)
    rows = [
        {"diagnostic_only": False, "column": name, "field": f"Field {name}", "after": "主值命中"}
        for name in columns
    ]

    def write(folder, name, value):
        (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    write(
        current,
        "verification.json",
        {
            "after": {"主值命中": len(columns)},
            "before": {"主值命中": 1},
            "groups": 1,
            "fixed_reads": 2,
            "still_missing_primary": 0,
            "elapsed_seconds": 2.5,
        },
    )
    write(current, "field-comparison.json", rows)
    write(timing, "intake.json", [{"email": "private input is not projected"}])
    write(
        timing, "latency-comparison.json", {"wall_s": {"stream": {"mean": 2, "p50": 2, "p95": 3}}}
    )
    result = evaluation.saved_evaluation(tmp_path)
    assert result["confirmed"]["hits"] == len(columns)
    assert result["cases"] == []
    assert "private input" not in json.dumps(result)
