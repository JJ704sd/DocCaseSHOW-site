"""Expose a bounded, source-linked estimate for one explicit package."""

import re
from decimal import Decimal
from pathlib import Path

from doccase_preprocessing.extraction_agent import current_body_text_check
from doccase_preprocessing.local_handoff import _read_content
from doccase_preprocessing.main_channel import _blocks, _current_body_blocks

_HEADING = re.compile(r"(?:cargo details|cargo summary|货物明细)\s*[:：]?", re.I)
_PACKAGE = re.compile(r"(?P<count>[1-9]\d{0,8})\s*(?P<label>packages?|pkgs?)\s*[:：.。]?", re.I)
_DIMENSIONS = re.compile(
    r"(?:dims?\.?|dimensions?|尺寸)\s*[:：]\s*"
    r"(?P<length>\d+(?:\.\d+)?)\s*[x×*]\s*"
    r"(?P<width>\d+(?:\.\d+)?)\s*[x×*]\s*"
    r"(?P<height>\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)\s*[.。]?",
    re.I,
)


def body_measurement(read, record, content_root):
    """Use the selected body READ; never promote an estimate to a stated volume."""
    if record.get("kind") != "body" or record.get("role") != "current":
        return None
    item = next((x for x in read["inventory"] if x["material_id"] == record["material_id"]), None)
    if not item or item["read_state"] not in {"success", "partial_success"}:
        return None
    execution = next(
        (x for x in read["executions"] if x.get("execution_id") == item["execution_id"]), None
    )
    if (
        not execution
        or not execution.get("ref")
        or execution["executed_profile"]["reader"] == "DoclingMarkdown"
    ):
        return None
    raw = _read_content(Path(content_root).resolve(), record["content"]).decode("utf-8-sig")
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    lines = []
    for block, end in _current_body_blocks(_blocks(execution)[1]):
        if block.get("cell"):
            lines.append(("", None))
            continue
        for match in re.finditer(r"[^\r\n]+", block["text"][:end]):
            text = match[0].strip()
            if text:
                start = match.start() + len(match[0]) - len(match[0].lstrip())
                lines.append(
                    (
                        text,
                        {
                            "excerpt": text,
                            "pointer": block["pointer"],
                            "start": start,
                            "end": start + len(text),
                            "kind": "current_body",
                        },
                    )
                )
    heads = [i for i, (text, _) in enumerate(lines) if _HEADING.fullmatch(text) and current(text)]
    if len(heads) != 1:
        return None
    start = heads[0]
    window = lines[start + 1 : start + 7]
    packages = [
        (i, match)
        for i, (text, _) in enumerate(window, start + 1)
        if (match := _PACKAGE.fullmatch(text)) and current(text)
    ]
    if len(packages) != 1:
        return None
    index, package = packages[0]
    if not current(" ".join(text for text, _ in lines[start : index + 1])):
        return None
    count = int(package["count"])
    package_source = lines[index][1]
    result = {
        "package": {"label": package["label"], "count": count, "sources": [package_source]},
        "volume_estimate": None,
    }
    if count != 1 or index + 1 >= len(lines):
        return result
    text, dimensions_source = lines[index + 1]
    dimensions = _DIMENSIONS.fullmatch(text)
    if not dimensions or not current(" ".join(line for line, _ in lines[start : index + 2])):
        return result
    values = [Decimal(dimensions[name]) for name in ("length", "width", "height")]
    if any(value <= 0 for value in values):
        return result
    unit = dimensions["unit"].lower()
    factor = {"mm": Decimal("0.001"), "cm": Decimal("0.01"), "m": Decimal(1)}[unit]
    volume = values[0] * values[1] * values[2] * factor**3
    result["volume_estimate"] = {
        "value": float(volume),
        "unit": "CBM",
        "status": "estimated",
        "dimensions": [str(value) for value in values],
        "dimension_unit": unit,
        "sources": [package_source, dimensions_source],
    }
    return result
