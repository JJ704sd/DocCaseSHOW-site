"""Source-bound shipment and cargo-row observations from one saved body READ."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from .extraction_agent import _QUOTE_HEADER, _visible

_NUMBER = r"\d+(?:[.,]\d+)*"
_DATA = re.compile(r"^\s*(?:data|cargo\s+details|shipment(?:\s*\d+)?)\s*[:：]\s*", re.I)
_COUNT = re.compile(
    rf"(?P<value>{_NUMBER})\s*(?P<unit>ctns?|cartons?|boxes?|bx|plts?|pallets?)\b", re.I
)
_WEIGHT = re.compile(rf"(?P<value>{_NUMBER})\s*(?:kgs?|kilograms?)\b", re.I)
_VOLUME = re.compile(rf"(?P<value>{_NUMBER})\s*(?:cbm|m[³3])\b", re.I)
_BARE_VOLUME = re.compile(rf"\s*[/|:]\s*(?P<value>{_NUMBER})\s*,\s*(?:cw|chargeable)\b", re.I)
_CHARGE = re.compile(
    rf"(?:\b(?:cw|chargeable(?:\s+weight)?)\s*[:=]?\s*(?P<a>{_NUMBER})\s*(?:kgs?)?\b|"
    rf"\b(?P<b>{_NUMBER})\s*(?:kgs?\s*)?\b(?:cw|chargeable)\b)",
    re.I,
)
_ROUTE = re.compile(
    r"\b(?:EXW|FCA|FOB|CFR|CIF|DAP|DDP)\s+(?P<origin>[A-Z]{3})\s*[-–>]\s*"
    r"(?P<destination>[A-Z]{3})\b",
    re.I,
)
_ALTERNATIVE = re.compile(r"\b(?:or|either|approximately|approx)\b|或者|或是|预计|可能", re.I)
_REF = re.compile(r"\b([A-Z]{2,})[- ]?(\d{2,})(?:[-/](\d{2,}))*\b", re.I)
_SHIPMENT_HEADING = re.compile(r"^\s*(?:shipment|cargo|货票|货件)\s*\d+\s*[:：]", re.I)
_TRADE_TERM = re.compile(r"\s*(?P<value>EXW|FCA|FOB|CFR|CIF|CPT|CIP|DAP|DDP)\s*[.。]?\s*", re.I)


def _number(text):
    text = text.strip().replace(" ", "")
    if "," in text and "." in text:
        text = (
            text.replace(",", "")
            if text.rfind(".") > text.rfind(",")
            else text.replace(".", "").replace(",", ".")
        )
    elif "," in text:
        if re.fullmatch(r"[1-9]\d{0,2},\d{3}", text):
            return None
        text = text.replace(",", ".")
    try:
        value = Decimal(text)
        return int(value) if value == value.to_integral_value() else float(value)
    except InvalidOperation:
        return None


def _id(kind, email_sha, material_sha, pointer):
    raw = json.dumps([email_sha, material_sha, pointer], ensure_ascii=False, separators=(",", ":"))
    return kind + ":" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _normal(text):
    return " ".join(str(text).split()).casefold()


def _source_scopes(original):
    visible = _visible(original)
    boundary = _QUOTE_HEADER.search(visible)
    current, quoted = (
        (visible[: boundary.start()], visible[boundary.start() :]) if boundary else (visible, "")
    )
    return _normal(current), _normal(quoted)


def _source_role(scopes, text):
    current, quoted = scopes
    needle = _normal(text)
    in_current = bool(needle and needle in current)
    in_quoted = bool(needle and needle in quoted)
    if in_current and in_quoted:
        return "unknown"
    if in_current:
        return "current_body"
    if in_quoted:
        return "historical_quote"
    return "unknown"


def _source(pointer, excerpt, role, **location):
    return {"pointer": pointer, "excerpt": excerpt, "kind": role, **location}


def _fact(value, unit, sources):
    return {"value": value, "unit": unit, "status": "known", "sources": sources}


def _ordered(document):
    children = document.get("body", {}).get("children", [])
    if not children:
        return [
            (f"/texts/{i}/text", item.get("text", ""))
            for i, item in enumerate(document.get("texts", []))
        ]
    result, seen, pending = [], set(), list(reversed(children))
    while pending:
        ref = pending.pop().get("$ref", "")
        if ref in seen:
            continue
        seen.add(ref)
        match = re.fullmatch(r"#/(texts|groups|tables)/(\d+)", ref)
        if not match:
            continue
        kind, index = match[1], int(match[2])
        items = document.get(kind, [])
        if index >= len(items):
            continue
        item = items[index]
        if kind == "groups":
            pending.extend(reversed(item.get("children", [])))
        elif kind == "texts":
            result.append((f"/texts/{index}/text", item.get("text", "")))
        else:
            result.append((f"/tables/{index}", item))
    return result


def _subject_refs(subject):
    refs = set()
    for match in _REF.finditer(subject or ""):
        token = match.group().upper().replace(" ", "")
        prefix, *numbers = re.split(r"[-/]", token)
        first = re.fullmatch(r"([A-Z]+)(\d+)", prefix)
        if first:
            letters, number = first.groups()
            refs.add(letters + number)
        else:
            letters = prefix
        refs.update(letters + number for number in numbers if number.isdigit())
    return refs


def _references(text):
    return {re.sub(r"[^A-Z0-9]", "", match.group().upper()) for match in _REF.finditer(text)}


def _adjacent_heading(scopes, heading, text):
    """Bind the READ heading only when it immediately precedes this unique current segment."""
    current = scopes[0]
    needle = _normal(text)
    start = current.find(needle)
    if start < 0 or current.find(needle, start + 1) >= 0:
        return False

    def normalized(value):
        return re.sub(r"\s*[:：]\s*", ":", _normal(value))

    before = current[max(0, start - len(_normal(heading)) - 12) : start]
    return normalized(before).endswith(normalized(heading))


def _adjacent_current(scopes, first, second):
    pair = _normal(first) + " " + _normal(second)
    current, quoted = scopes
    start = current.find(pair)
    end = start + len(pair)
    return (
        start >= 0
        and current.find(pair, start + 1) < 0
        and pair not in quoted
        and (end == len(current) or not (current[end].isalnum() or current[end] == "_"))
    )


def body_objects(document, original, *, email_sha, material_sha, subject=""):
    """Return independently located facts; never turn quoted values into current facts."""
    if not isinstance(document, dict) or not isinstance(original, str):
        return {"shipments": [], "packing_rows": []}
    shipments, lines, context, route, volume_example = [], [], [], None, None
    scopes = _source_scopes(original)
    refs = _subject_refs(subject)
    ordered = _ordered(document)
    for position, (pointer, item) in enumerate(ordered):
        if isinstance(item, str):
            text = item.strip()
            role = _source_role(scopes, text)
            context.append((pointer, text, role))
            if len(context) > 12:
                context.pop(0)
            route_match = _ROUTE.search(text)
            if route_match:
                route = (
                    (route_match, _source(pointer, route_match.group(), role))
                    if role == "current_body"
                    else None
                )
            head = _DATA.match(text)
            if route and text and not head and not route_match and role == "current_body":
                route = None
            if text and not head and not route_match and role == "current_body":
                volume_example = None
            count = _COUNT.search(text, head.end()) if head else None
            weight = _WEIGHT.search(text, count.end()) if count else None
            if not (head and count and weight and role == "current_body") or _ALTERNATIVE.search(
                text
            ):
                continue
            between = text[count.end() : weight.start()].strip(" /|:,-")
            if between and not re.fullmatch(
                r"(?:gross(?:\s+weight)?|g\.?w\.?)\s*[:=]?", between, re.I
            ):
                continue
            tail = text[weight.end() :]
            explicit_volume = _VOLUME.search(tail)
            bare_volume = _BARE_VOLUME.match(tail) if not explicit_volume else None
            volume = explicit_volume or (bare_volume if volume_example else None)
            count_value, weight_value = _number(count["value"]), _number(weight["value"])
            if (
                any(v is None or v <= 0 for v in (count_value, weight_value))
                or int(count_value) != count_value
            ):
                continue
            source = _source(pointer, text, role)
            label = count["unit"].casefold()
            is_carton = label.startswith("ctn") or label.startswith("carton")
            is_pallet = label.startswith("plt") or label.startswith("pallet")
            weight_field = "shipment_gross_weight" if between else "shipment_actual_weight"
            fields = {
                ("ctns" if is_carton else "package_count"): _fact(
                    count_value,
                    "carton" if is_carton else "pallet" if is_pallet else "box",
                    [source],
                ),
                weight_field: _fact(weight_value, "kg", [source]),
            }
            if is_pallet:
                fields["package_type"] = _fact("pallet", None, [source])
            if volume:
                volume_value = _number(volume["value"])
                if volume_value is not None and volume_value > 0:
                    fields["shipment_volume"] = _fact(
                        volume_value,
                        "CBM",
                        [source] if explicit_volume else [source, volume_example],
                    )
            if explicit_volume:
                volume_example = source
            if match := _CHARGE.search(tail):
                charge = _number(match["a"] or match["b"])
                if charge is not None:
                    fields["shipment_chargeable_weight"] = _fact(charge, "kg", [source])
            if route:
                fields["departure_port"] = _fact(
                    route[0]["origin"].upper(), None, [route[1], source]
                )
                fields["destination"] = _fact(
                    route[0]["destination"].upper(), None, [route[1], source]
                )
            association = {"status": "local_segment"}
            heading = next(((p, t, r) for p, t, r in reversed(context[:-1]) if t.strip()), None)
            if heading and _SHIPMENT_HEADING.match(heading[1]):
                matches = _references(heading[1]) & refs
                if len(matches) == 1 and _adjacent_heading(scopes, heading[1], text):
                    association = {
                        "status": "verified",
                        "basis": "subject_reference",
                        "scope": "shipment_identifier_only",
                        "currentness": "unverified",
                        "reference": next(iter(matches)),
                        "sources": [
                            {"kind": "eml_header", "field": "subject", "excerpt": subject},
                            _source(heading[0], heading[1], role),
                        ],
                    }
            shipment = {
                "object_id": _id("shipment", email_sha, material_sha, pointer),
                "source_role": role,
                "location": {"pointer": pointer},
                "association": association,
                "direct_fields": fields,
            }
            if position + 1 < len(ordered):
                following_pointer, following = ordered[position + 1]
                if (
                    isinstance(following, str)
                    and (term := _TRADE_TERM.fullmatch(following))
                    and _adjacent_current(scopes, text, following)
                ):
                    fields["trade_terms"] = _fact(
                        term["value"].upper(),
                        None,
                        [source, _source(following_pointer, following.strip(), role)],
                    )
            shipments.append(shipment)
            continue
        route, volume_example = None, None
        data = item.get("data", {})
        cells = data.get("table_cells", [])
        headers = {}
        for index, cell in enumerate(cells):
            text = " ".join(str(cell.get("text", "")).split()).casefold()
            name = (
                "line_net_weight"
                if re.search(r"\bnet\s+weight\b", text)
                else "line_gross_weight"
                if re.search(r"\bgross\s+weight\b", text)
                else "line_volume"
                if re.search(r"\bvolume\b", text)
                else None
            )
            if name and (
                (name == "line_volume" and re.search(r"\bm[³3]\b|\bcbm\b", text))
                or (name != "line_volume" and re.search(r"\bkgs?\b", text))
            ):
                headers[name] = (cell.get("start_col_offset_idx"), index, cell)
        if set(headers) != {"line_net_weight", "line_gross_weight", "line_volume"}:
            continue
        rows = {}
        for index, cell in enumerate(cells):
            row, col = cell.get("start_row_offset_idx"), cell.get("start_col_offset_idx")
            if type(row) is int and type(col) is int:
                rows.setdefault(row, {})[col] = (index, cell)
        section = next(
            (
                (p, t, r, next(iter(matches)))
                for p, t, r in reversed(context)
                if len(matches := _references(t) & refs) == 1
            ),
            None,
        )
        table_shipment = None
        for row_index, row in sorted(rows.items()):
            values = [row.get(headers[name][0]) for name in headers]
            nums = [_number(str(value[1].get("text", ""))) if value else None for value in values]
            # Numeric dimension cells establish an individual row; exclude blank totals.
            dimensions = [row.get(i) for i in range(3)]
            if not any(v is not None and v > 0 for v in nums) or any(
                not cell or _number(str(cell[1].get("text", ""))) is None for cell in dimensions
            ):
                continue
            row_text = " ".join(str(row[k][1].get("text", "")).strip() for k in sorted(row))
            role = _source_role(scopes, row_text)
            if role == "unknown":
                continue
            if table_shipment is None:
                association = {"status": "unverified"}
                if section and section[3] in refs and section[2] == role:
                    association = {
                        "status": "verified",
                        "basis": "subject_reference",
                        "scope": "shipment_identifier_only",
                        "currentness": "unverified",
                        "reference": section[3],
                        "sources": [
                            {"kind": "eml_header", "field": "subject", "excerpt": subject},
                            _source(section[0], section[1], role),
                        ],
                    }
                table_shipment = {
                    "object_id": _id("shipment", email_sha, material_sha, pointer),
                    "source_role": role,
                    "location": {"pointer": pointer},
                    "association": association,
                    "subject_references": sorted(refs),
                    "direct_fields": {},
                }
                shipments.append(table_shipment)
            if role != table_shipment["source_role"]:
                continue
            fields, evidence_spans = {}, []
            for name, (col, header_index, header) in headers.items():
                header_source = _source(
                    f"{pointer}/data/table_cells/{header_index}/text",
                    header["text"],
                    role,
                    table=pointer,
                    row=header.get("start_row_offset_idx", 0) + 1,
                    column=col + 1,
                )
                evidence_spans.append({**header_source, "field": name, "position": "header"})
                if col not in row:
                    continue
                cell_index, cell = row[col]
                cell_source = _source(
                    f"{pointer}/data/table_cells/{cell_index}/text",
                    cell["text"],
                    role,
                    table=pointer,
                    row=row_index + 1,
                    column=col + 1,
                )
                evidence_spans.append({**cell_source, "field": name, "position": "value"})
                value = _number(str(cell["text"]))
                if value is not None and value > 0:
                    fields[name] = _fact(
                        value,
                        "CBM" if name == "line_volume" else "kg",
                        [cell_source, header_source],
                    )
            lines.append(
                {
                    "object_id": _id(
                        "cargo-line", email_sha, material_sha, f"{pointer}/row/{row_index + 1}"
                    ),
                    "shipment_id": table_shipment["object_id"],
                    "source_role": role,
                    "association": table_shipment["association"],
                    "table": pointer,
                    "row": row_index + 1,
                    "row_evidence_hash": hashlib.sha256(
                        json.dumps(
                            [(col, _normal(row[col][1].get("text", ""))) for col in sorted(row)],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "ordinal": len(
                        [x for x in lines if x["shipment_id"] == table_shipment["object_id"]]
                    )
                    + 1,
                    "fields": fields,
                    "evidence_spans": evidence_spans,
                }
            )
        context = []
    return {"shipments": shipments, "packing_rows": lines}
