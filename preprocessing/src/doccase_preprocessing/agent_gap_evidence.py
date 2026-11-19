"""Exact current-body spans for Agent-only recovery of common compact replies."""

import re
from collections import defaultdict
from decimal import Decimal

_NUMBER = r"\d+(?:[.,]\d+)?"
_FREIGHT = re.compile(
    rf"(?P<count>[1-9]\d*)[ \t]*(?P<package>ctns?|cartons?|pallets?|托盘)[ \t]*"
    rf"(?:[/,，][ \t]*)?(?:{_NUMBER}[ \t]*[x×*][ \t]*{_NUMBER}[ \t]*[x×*][ \t]*{_NUMBER}[ \t]+)?"
    rf"{_NUMBER}[ \t]*kgs?[ \t,，/]+(?P<volume>{_NUMBER})[ \t]*(?:cbm|m[³3])[.。]?",
    re.I,
)
_QTY = re.compile(
    r"qty[ \t]*[:：][ \t]*[1-9]\d*[ \t]+(?P<package>cartons?|ctns?|pallets?)[.。]?", re.I
)
_TERM_CODE = r"EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CIF"
_TERM_TOKEN = re.compile(rf"\b(?:{_TERM_CODE})\b", re.I)
_TERM = re.compile(
    r"(?:[1-9]\d*[.)、][ \t]*)?(?P<proof>(?:贸易)?条款(?:为|[:：])[ \t]*"
    rf"(?P<term>{_TERM_CODE})"
    r"(?:[ \t]+[a-z]+(?:[ \t]+[a-z]+){0,2})?)(?:[.。][ \t]*(?:需要安排上门提货[^\r\n]*)?)?",
    re.I,
)
_UNSAFE = re.compile(
    r"\b(?:if|unless|whether|either|or|alternative|tentative|previous|not|per|each)\b"
    r"|如果|是否|还是|或|之前|原先|不是|每箱|每托|仅供|备选|暂定|待定|待确认|按.+报价",
    re.I,
)
_SHIPMENT = re.compile(r"\bshipment[ \t]*[1-9]\d*[ \t]*[:：]", re.I)
_CELL = re.compile(r"(?P<table>/tables/\d+)/data/table_cells/\d+/text")
_QUOTE_DATA = re.compile(rf"(?-i:[A-Z]{{3}})[ \t]+{_FREIGHT.pattern}", re.I)
_CARGO_HEADING = re.compile(
    r"(?:本票)?货物(?:信息|资料|数据|明细)|cargo (?:details|information|summary)", re.I
)
_ROUTE = re.compile(r"[A-Z]{3}[ \t]*[-–][ \t]*[A-Z]{3}")
_DIMENSIONS = re.compile(
    rf"{_NUMBER}\s*[x×*]\s*{_NUMBER}\s*[x×*]\s*{_NUMBER}\s*(?:mm|cm|m)",
    re.I,
)
_WEIGHT_LINE = re.compile(rf"(?:(?:(?:gross )?weight|毛重)\s*[:：]\s*)?{_NUMBER}\s*kgs?", re.I)
_PACK_LINE = re.compile(r"[1-9]\d*[ \t]*(?P<package>cartons?|ctns?|pallets?|托盘)", re.I)
_VOLUME_LINE = re.compile(rf"(?P<volume>{_NUMBER})[ \t]*(?:cbm|m[³3])", re.I)
_CARGO_INTRO = re.compile(
    r"(?:please (?:your kind )?support for |please (?:check|quote) )?(?:the )?"
    r"(?:below|following) cargo(?: to [a-z ,.-]+)?\s*[:：]",
    re.I,
)
_EU_WEIGHT = re.compile(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?\s*kgs?", re.I)
_DECLARATION = re.compile(
    r"(?P<label>destination(?: port| airport)?|port of discharge|POD|AOD|destino|puerto de destino|"
    r"目的地|目的港|目的机场|(?:trade |shipping )?terms|incoterms?|贸易条款|"
    r"(?:package|packaging|packing) type|tipo de embalaje|包装类型|"
    r"(?:cargo|commodity) type|tipo de carga|货物类型)\s*[:：]\s*(?P<value>.*)",
    re.I,
)
_PLACE = re.compile(r"[^\W\d_]+(?:[ .,'’\-]+[^\W\d_]+){0,7}", re.UNICODE)
_NOT_PLACE = re.compile(
    r"\b(?:tbd|pending|unknown|not confirmed|n/a|via|transit|option|either)\b|待定|待确认|中转",
    re.I,
)
_GROUP_CONTEXT_LIMIT = re.compile(
    r"\b(?:quote (?:is |only |valid |based )|rates? (?:are |based )|"
    r"for (?:reference|comparison) only|subject to confirmation|option [a-z0-9])\b"
    r"|仅作参考|报价适用|报价条件|备选|以确认(?:为准)?",
    re.I,
)
_TYPED_PACKAGE = {
    "carton": r"(?:(?:corrugated |cardboard )?cartons?|ctns?|cajas de cart[oó]n|纸箱)",
    "pallet": r"(?:pallets?|plts?|pal[eé]s?|tarimas?|托盘)",
    "wooden_case": r"(?:wooden (?:cases?|crates?|boxes)|cajas de madera|木箱)",
}
_TYPED_CARGO = {
    "general cargo": r"(?:general cargo|non[- ]dangerous goods|carga general|普货|普通货物)",
    "dangerous goods": r"(?:dangerous goods|mercanc[ií]as peligrosas|危险品|危品)",
}
_CARGO_MEASUREMENT_LABEL = re.compile(
    r"(?:volumen por caja|volumen total|total volume|volume per carton|(?:总|单箱)体积)\s*[:：].+",
    re.I,
)
_DECLARED_PACKING_LINE = re.compile(
    rf"(?:cargo details|cargo summary|货物明细)\s*[:：]\s*[1-9]\d*\s*"
    rf"(?P<package>{'|'.join(_TYPED_PACKAGE.values())})[ ,/，]*{_NUMBER}\s*kgs?[ ,/，]+"
    rf"{_NUMBER}\s*(?:cbm|m[³3])(?:\s+(?:1\s*:\s*\d+|(?:cw|chargeable weight)\s*:\s*{_NUMBER}\s*kgs?))*"
    r"\s*[.。]?",
    re.I,
)
_PACKING_LEAD = re.compile(r"(?:cargo details|cargo summary|货物明细)\s*[:：]", re.I)
SHIPMENT_SUMMARY_FIELDS = frozenset({"package_type", "line_volume", "shipment_gross_weight"})
_SUMMARY_NUMBER = r"[0-9]{1,12}(?:\.[0-9]{1,6}|,[0-9]{1,2})?"
_ONE_SHIPMENT = re.compile(
    rf"[^\r\n]{{0,70}}一票货物[^\r\n]{{0,45}}数据为\s*[:：]\s*[1-9]\d{{0,8}}\s*"
    rf"(?P<package>{'|'.join(_TYPED_PACKAGE.values())})\s*[,，/]\s*"
    rf"{_SUMMARY_NUMBER}\s*kgs?\s*[,，/]\s*(?P<volume>{_SUMMARY_NUMBER})\s*(?:cbm|m[³3])[.。]?",
    re.I,
)
_SHORT_GROSS = re.compile(
    rf"(?:G\.?\s*W\.?|gross weight|毛重)\s*[:：]\s*(?P<kg>{_SUMMARY_NUMBER})\s*kgs?\.?", re.I
)
_SUMMARY_DIMENSIONS = re.compile(
    rf"(?:dims?\.?|dimensions?|尺寸)\s*[:：]\s*{_NUMBER}\s*[x×*]\s*"
    rf"{_NUMBER}\s*[x×*]\s*{_NUMBER}\s*(?:mm|cm|m)",
    re.I,
)
_PACKAGE_BRIDGE = re.compile(
    rf"[1-9]\d*\s*(?:cajas|cartons?|ctns?|pallets?|托盘|纸箱)\s*[-–/]\s*{_NUMBER}\s*kgs?"
    r"(?:\s+(?:c/u|each))?",
    re.I,
)
_PACKAGE_COUNT_LINE = re.compile(
    r"(?:(?P<label>(?:total |number of )?(?:packages|pkgs)|(?:total )?package count|"
    r"(?:número de )?bultos|包装件数|总包装件数)\s*[:：]\s*)?"
    r"(?P<count>[1-9][0-9]{0,14})(?:\s*(?P<unit>packages?|pkgs?|bultos?|件))?\s*[:：.。]?",
    re.I,
)
_PACKAGE_COUNT_QUALIFIER = re.compile(
    r"\b(?:if|unless|additional|extra|remaining|previous|originally|alternative|tentative|"
    r"or|per|each|partial|only for|instead of|not confirmed|to be confirmed)\b"
    r"|如果|备选|加单|额外|剩余|原先|分批|每箱|每托|暂定|待确认",
    re.I,
)


def _category(value, patterns):
    return next(
        (key for key, pattern in patterns.items() if re.fullmatch(pattern, value, re.I)), None
    )


def shipment_summary_evidence(scoped, current):
    """Prove a typed shipment summary, never an isolated or per-package measurement."""
    lines = []
    for block, end in scoped:
        if block.get("cell"):
            lines.append(("", None))
            continue
        for m in re.finditer(r"[^\r\n]+", block["text"][:end]):
            if text := m[0].strip():
                start = m.start() + len(m[0]) - len(m[0].lstrip())
                lines.append((text, (block, start, start + len(text))))
    anchors = [
        (i, _ONE_SHIPMENT.fullmatch(text))
        for i, (text, _) in enumerate(lines)
        if current(text) and (_ONE_SHIPMENT.fullmatch(text) or _PACKING_LEAD.fullmatch(text))
    ]
    if len(anchors) != 1 or any(_SHIPMENT.fullmatch(text) and current(text) for text, _ in lines):
        return
    i, inline = anchors[0]
    text, heading = lines[i]
    near = [s for s, _ in lines[max(0, i - 1) : i + 2]]
    # A following question about readiness or terms does not qualify a complete cargo declaration.
    previous = lines[i - 1][0] if i else ""
    if (
        _UNSAFE.search(text)
        or (previous.endswith((":", "：")) and _UNSAFE.search(previous))
        or any(_GROUP_CONTEXT_LIMIT.search(s) for s in near)
    ):
        return
    if inline:
        yield "package_type", _category(inline["package"], _TYPED_PACKAGE), None, [heading]
        yield "line_volume", float(Decimal(inline["volume"].replace(",", "."))), "CBM", [heading]
        return
    weights = []
    for j in range(i + 1, min(i + 6, len(lines))):
        line, loc = lines[j]
        if not loc or not current(line):
            break
        if weight := _SHORT_GROSS.fullmatch(line):
            if current(" ".join(s for s, _ in lines[i : j + 1])):
                weights.append((Decimal(weight["kg"].replace(",", ".")), loc))
            continue
        if not (
            _category(line, _TYPED_CARGO)
            or _PACKAGE_COUNT_LINE.fullmatch(line)
            or _PACK_LINE.fullmatch(line)
            or _SUMMARY_DIMENSIONS.fullmatch(line)
        ):
            break
    if len(weights) == 1:
        kg, value_loc = weights[0]
        yield (
            "shipment_gross_weight",
            int(kg) if kg == kg.to_integral_value() else float(kg),
            "kg",
            [
                value_loc,
                heading,
            ],
        )


def _generic_package_count(lines, current, packing_lines):
    """Prove a generic count without treating the package unit as a carton type."""
    counts = [
        (i, m)
        for i, (text, _) in enumerate(lines)
        if (m := _PACKAGE_COUNT_LINE.fullmatch(text))
        and (m["label"] or m["unit"] not in {None, "件"})
        and current(text)
    ]
    if len(counts) != 1 or len(packing_lines) > 1:
        return None
    i, match = counts[0]
    neighbors = [text for text, _ in lines[max(0, i - 2) : i + 2]]
    if any(
        _PACKAGE_COUNT_QUALIFIER.search(text) or _GROUP_CONTEXT_LIMIT.search(text)
        for text in neighbors
    ):
        return None
    text, loc = lines[i]
    value = int(match["count"])
    if match["label"]:
        return value, [loc]
    # A generic number alone needs the current cargo panel. A cargo category
    # may appear between the panel heading and its package count.
    for j in range(max(0, i - 2), i):
        heading, anchor = lines[j]
        if (
            anchor
            and _CARGO_HEADING.fullmatch(heading.rstrip(":：").rstrip())
            and all(_category(line, _TYPED_CARGO) for line, _ in lines[j + 1 : i])
            and current(" ".join(line for line, _ in lines[j : i + 1]))
        ):
            return value, [anchor, loc]
    return None


def group_declaration_evidence(blocks, current):
    """Exact current-body declarations, including a label and value split by layout."""
    lines = []
    for block in blocks:
        if block.get("cell"):
            lines.append(("", None))
            continue
        for match in re.finditer(r"[^\r\n]+", block["text"]):
            if text := match[0].strip():
                a = match.start() + len(match[0]) - len(match[0].lstrip())
                lines.append((text, (block, a, a + len(text))))
    # Two explicit shipment panels cannot be silently reduced to one document field.
    if sum(bool(_SHIPMENT.fullmatch(text) and current(text)) for text, _ in lines) > 1:
        return
    by_field = defaultdict(dict)
    packing_lines = [
        i for i, (text, _) in enumerate(lines) if _PACKING_LEAD.match(text) and current(text)
    ]
    if count := _generic_package_count(lines, current, packing_lines):
        yield "package_count", count[0], None, count[1]
    for i, (text, loc) in enumerate(lines):
        previous = lines[i - 1][0] if i else ""
        decl = _DECLARATION.fullmatch(text)
        packing = _DECLARED_PACKING_LINE.fullmatch(text) if packing_lines == [i] else None
        cargo = _category(text, _TYPED_CARGO) if not decl else None
        if not (decl or packing or cargo):
            continue
        label, val = (decl["label"].casefold(), decl["value"].strip()) if decl else ("", text)
        proof = [loc]
        if decl and not val and i + 1 < len(lines):
            val, value_loc = lines[i + 1]
            if value_loc is None:
                continue
            proof.append(value_loc)
        scope = text if len(proof) == 1 else text + " " + val
        after = i + len(proof)
        following = lines[after][0] if after < len(lines) else ""
        if (
            not val
            or _UNSAFE.search(scope)
            or not current(scope)
            or any(_GROUP_CONTEXT_LIMIT.search(x) for x in (previous, scope, following))
        ):
            continue
        if not decl:
            if packing:
                field = "package_type"
                value = _category(packing["package"], _TYPED_PACKAGE)
            elif cargo:
                field = "cargo_type"
                value = cargo
                if (
                    i
                    and _CARGO_HEADING.fullmatch(previous.rstrip(":：").rstrip())
                    and current(previous + " " + text)
                ):
                    proof = [lines[i - 1][1], loc]
                elif (
                    after < len(lines)
                    and _CARGO_MEASUREMENT_LABEL.fullmatch(following)
                    and current(text + " " + following)
                ):
                    proof = [lines[after][1], loc]
                elif (
                    after + 1 < len(lines)
                    and _PACKAGE_BRIDGE.fullmatch(following)
                    and _CARGO_MEASUREMENT_LABEL.fullmatch(lines[after + 1][0])
                    and current(lines[after + 1][0])
                ):
                    # A layout-only split can interpose an explicit package
                    # count/weight line. It is never used as type or volume evidence.
                    proof = [lines[after + 1][1], loc]
                else:
                    continue
            else:
                continue
        elif label in {
            "terms",
            "trade terms",
            "shipping terms",
            "incoterm",
            "incoterms",
            "贸易条款",
        }:
            field, value = "trade_terms", val.upper().rstrip(".")
            if not _TERM_TOKEN.fullmatch(value):
                continue
        elif label in {
            "package type",
            "packaging type",
            "packing type",
            "tipo de embalaje",
            "包装类型",
        }:
            field = "package_type"
            value = _category(val, _TYPED_PACKAGE)
        elif label in {"cargo type", "commodity type", "tipo de carga", "货物类型"}:
            field = "cargo_type"
            value = _category(val, _TYPED_CARGO)
        else:
            field, value = "destination", val
            if len(val) > 75 or not _PLACE.fullmatch(val) or _NOT_PLACE.search(val):
                continue
        if value:
            by_field[field].setdefault(value, proof)
    for field, values in by_field.items():
        if len(values) == 1:
            value, proof = next(iter(values.items()))
            yield field, value, None, proof


def _package(text):
    return "pallet" if text.casefold().startswith("pallet") or text == "托盘" else "carton"


def compact_cargo_context(text, start, end, current):
    """A bounded same-block heading/route window for an already proven cargo line."""
    # Context may identify the shipment, but cannot carry a second value.
    before = [line for line in re.finditer(r"[^\r\n]+", text[:start]) if line[0].strip()]
    selected = []
    for line in reversed(before[-3:]):
        t = line[0].strip()
        if (
            _CARGO_HEADING.fullmatch(t.rstrip(":：").rstrip())
            or _SHIPMENT.fullmatch(t)
            or _ROUTE.fullmatch(t)
        ):
            selected.append(line)
        else:
            break
    if not selected or not any(
        _CARGO_HEADING.fullmatch(line[0].strip().rstrip(":：").rstrip()) for line in selected
    ):
        return None
    first = selected[-1]
    left = first.start() + len(first[0]) - len(first[0].lstrip())
    excerpt = text[left:end]
    return (left, end) if len(excerpt) <= 220 and current(excerpt) else None


def compact_gap_evidence(text, current):
    """Yield (field, value, unit, start, end) for a complete, in-scope READ line."""
    # A locally clipped line may not hide a second shipment or a condition.
    if len(_SHIPMENT.findall(text)) > 1:
        return
    lines = list(re.finditer(r"[^\r\n]+", text))
    for i, line in enumerate(lines):
        cleaned = line[0].strip()
        start = line.start() + len(line[0]) - len(line[0].lstrip())
        end = start + len(cleaned)
        if not cleaned or not current(cleaned) or _UNSAFE.search(cleaned):
            continue
        context = lines[max(0, i - 1) : i] + lines[i + 1 : i + 2]
        if any(_UNSAFE.search(x[0]) for x in context):
            continue
        if match := _FREIGHT.fullmatch(cleaned):
            yield "package_type", _package(match["package"]), None, start, end
            volume = Decimal(match["volume"].replace(",", "."))
            yield "line_volume", float(volume), "CBM", start, end
        elif match := _QTY.fullmatch(cleaned):
            yield "package_type", _package(match["package"]), None, start, end
        elif (match := _TERM.fullmatch(cleaned)) and len(_TERM_TOKEN.findall(cleaned)) == 1:
            a, b = match.span("proof")
            yield "trade_terms", match["term"].upper(), None, start + a, start + b


def quote_table_gap_evidence(blocks, current):
    """One explicitly separated cargo-data column may support supplement candidates."""
    tables = defaultdict(list)
    for block in blocks:
        if block.get("cell") and (ref := _CELL.fullmatch(block["pointer"])):
            tables[ref["table"]].append(block)
    eligible = []
    for cells in tables.values():
        head = [b for b in cells if b["cell"]["start_row_offset_idx"] == 0]
        if len(head) != 3 or any(type(b["cell"]["start_col_offset_idx"]) is not int for b in head):
            continue
        head.sort(key=lambda b: b["cell"]["start_col_offset_idx"])
        if any(
            (
                b["cell"]["start_col_offset_idx"],
                b["cell"]["end_col_offset_idx"],
                b["cell"]["end_row_offset_idx"],
                b["text"].strip(),
            )
            != (i, i + 1, 1, label)
            for i, (b, label) in enumerate(zip(head, ("数据", "成本", "渠道"), strict=True))
        ):
            continue
        # A vertically merged data cell can describe a single shipment across
        # several cost rows. A second data value or a horizontal merge cannot.
        data = [
            b
            for b in cells
            if b["cell"]["start_row_offset_idx"] > 0
            and b["cell"]["start_col_offset_idx"] == 0
            and b["text"].strip()
        ]
        if len(data) != 1:
            continue
        cargo = data[0]
        if cargo["cell"]["start_row_offset_idx"] != 1 or cargo["cell"]["end_col_offset_idx"] != 1:
            continue
        if not current(" ".join(b["text"] for b in [*head, cargo])):
            continue
        eligible.append((head[0], cargo))
    if len(eligible) != 1:
        return
    label, cargo = eligible[0]
    lines = [line.strip() for line in cargo["text"].splitlines() if line.strip()]
    if (
        not lines
        or not (match := _QUOTE_DATA.fullmatch(lines[0]))
        or len(lines) > 2
        or (len(lines) == 2 and not _DIMENSIONS.fullmatch(lines[1]))
    ):
        return
    proof = [(b, 0, len(b["text"])) for b in (label, cargo)]
    yield "package_type", _package(match["package"]), None, proof
    # A dimension continuation may refer to an individual package. It does
    # not change the package type, but the volume level remains unresolved.
    if len(lines) == 1:
        yield "line_volume", float(Decimal(match["volume"].replace(",", "."))), "CBM", proof


def split_cargo_gap_evidence(blocks, current):
    """Bind volume across a current cargo heading and contiguous measurement blocks."""
    visible = [b for b in blocks if b["text"].strip()]
    heads = [
        i
        for i, b in enumerate(visible)
        if not b.get("cell") and _CARGO_HEADING.fullmatch(b["text"].strip().rstrip(":：").rstrip())
    ]
    if not heads:
        yield from _cargo_summary_panel(visible, current)
        return
    if len(heads) != 1:
        return
    start = heads[0]
    cluster = visible[start : start + 5]
    if (
        len(cluster) < 3
        or cluster[1].get("cell")
        or not _PACK_LINE.fullmatch(cluster[1]["text"].strip())
    ):
        return
    seen = set()
    for i, b in enumerate(cluster[2:], 2):
        if b.get("cell"):
            return
        text = b["text"].strip()
        if match := _VOLUME_LINE.fullmatch(text):
            following = visible[start + i + 1 : start + i + 2]
            if following and (
                _UNSAFE.search(following[0]["text"])
                or _VOLUME_LINE.fullmatch(following[0]["text"].strip())
            ):
                return
            if current(" ".join(x["text"].strip() for x in cluster[: i + 1])):
                proof = [(x, 0, len(x["text"])) for x in (cluster[0], cluster[1], b)]
                yield "line_volume", float(Decimal(match["volume"].replace(",", "."))), "CBM", proof
            return
        kind = (
            "dimensions"
            if _DIMENSIONS.fullmatch(text)
            else "weight"
            if _WEIGHT_LINE.fullmatch(text)
            else None
        )
        if not kind or kind in seen:
            return
        seen.add(kind)


def _cargo_summary_panel(blocks, current):
    """A current cargo introduction, one package line and one measurement block."""
    leads = [
        i
        for i, b in enumerate(blocks)
        if not b.get("cell") and _CARGO_INTRO.fullmatch(b["text"].strip())
    ]
    if len(leads) != 1:
        return
    lead = leads[0]
    panel = blocks[lead : lead + 4]
    for i in (1, 2):
        if len(panel) <= i + 1 or any(b.get("cell") for b in panel[: i + 2]):
            continue
        pack, measurements = panel[i : i + 2]
        packaging = [line.strip() for line in pack["text"].splitlines() if line.strip()]
        lines = list(re.finditer(r"[^\r\n]+", measurements["text"]))
        if (
            not packaging
            or len(packaging) > 2
            or not _PACK_LINE.fullmatch(packaging[0])
            or (len(packaging) == 2 and not _DIMENSIONS.fullmatch(packaging[1]))
            or len(lines) != 2
            or not (volume := _VOLUME_LINE.fullmatch(lines[0][0].strip()))
            or not _EU_WEIGHT.fullmatch(lines[1][0].strip())
        ):
            continue
        scope = " ".join(b["text"].strip() for b in panel[: i + 2])
        if _UNSAFE.search(scope) or _SHIPMENT.search(scope) or not current(scope):
            return
        # A second package or measurement in the same nearby panel is not a total.
        neighbors = panel[1:i] + blocks[lead + i + 2 : lead + i + 4]
        if any(
            _UNSAFE.search(b["text"])
            or any(
                _PACK_LINE.fullmatch(line.strip()) or _VOLUME_LINE.fullmatch(line.strip())
                for line in b["text"].splitlines()
            )
            for b in neighbors
        ):
            return
        heading = panel[0]
        a = lines[0].start() + len(lines[0][0]) - len(lines[0][0].lstrip())
        z = a + len(lines[0][0].strip())
        proof = [
            (heading, 0, len(heading["text"])),
            (pack, 0, len(pack["text"])),
            (measurements, a, z),
        ]
        yield "line_volume", float(Decimal(volume["volume"].replace(",", "."))), "CBM", proof
        return
