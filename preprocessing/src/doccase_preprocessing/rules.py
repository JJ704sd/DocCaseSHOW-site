"""Deterministic rules shared by extraction and fixed-evidence revision."""

from decimal import Decimal


def evaluate_candidates(candidates, object_selector):
    """Evaluate equal-carton gross weight from one unambiguous pair of candidates.

    This calculation does not establish that weights are equal; callers must have
    independently established and retained the corresponding source evidence.
    """
    parents = {}
    for name in ("ctns", "line_gross_weight"):
        matches = [
            item
            for item in candidates
            if item["field_name"] == name and item["object_id"] == object_selector
        ]
        if not matches or any(item["field"]["status"] != "known" for item in matches):
            return None
        if len({str(item["field"]["value"]) for item in matches}) != 1:
            return None
        parents[name] = matches[0]
    count = Decimal(str(parents["ctns"]["field"]["value"]))
    total = Decimal(str(parents["line_gross_weight"]["field"]["value"]))
    if not count.is_finite() or not total.is_finite() or count <= 0:
        return None
    value = total / count
    if value * count != total:
        return None
    return {
        "value": int(value) if value == value.to_integral_value() else float(value),
        "parents": parents,
    }
