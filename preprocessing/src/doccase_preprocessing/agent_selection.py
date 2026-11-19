"""Plan bounded Agent requests from rule results and already verified evidence."""

import re
from collections import Counter
from copy import deepcopy

GROUP_AGENT_INSTRUCTION_SUFFIX = (
    "\n补提口径：只在引用证据能确定当前货票和请求字段时输出。"
    "公司名只取公司的文字，不能包含部门或职位；联系人须引用姓名和对应的签名角色，"
    "不得用邮件头推断签名邮箱或联系人。货好日期与相对时段、已货好状态分开，"
    "不要把状态当日期、把相对时段猜成绝对日期，或把放货条件当货好时间。"
    "备注完整保留否定、条件和适用货票；排除报价费用、通用条款、历史转引。"
    "包装件数和包装类型独立：Package 只能证明泛称，不能推成纸箱；托盘数不能写成箱数。"
    "单件、货行与整票的数字和证据不得串用。"
    "字段标签和值分散在相邻片段时可联合判断，同时引用标签、值和必要的范围限定；"
    "两层表头要连同叶列和实际货行一起引用。不能从相邻货行借数。"
)


_FREIGHT_LEADS = {
    field: re.compile(expression, re.I)
    for field, expression in {
        "cargo_origin": r"\b(?:from|origin|ex[ -]works|exw|pickup)\b|货源|货在|起运|提货",
        "pickup_city": r"\b(?:pick[ -]?up|collection|exw|loading address)\b|提货|装货",
        "departure_port": r"\b(?:pol|aol|port of loading|departure (?:port|airport)|from)\b|起运[港地]|始发",
        "destination": r"\b(?:pod|aod|destination|destino|to|bound for|deliver to)\b|目的|到港|到达|(?:发|运|送|到)[\u4e00-\u9fff]",
        "trade_terms": r"\b(?:EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CNF|CIF)\b|贸易条款",
        "cargo_type": r"\b(?:general cargo|hazardous|non[ -]?dg|dg cargo|battery|dangerous goods|tipo de carga|carga general|mercanc[ií]as peligrosas)\b|普货|危品|危险品|带电|电池",
        "package_type": r"\b(?:cartons?|ctns?|boxes?|pallets?|plts?|crates?|packages?|wooden|bare cargo|unpacked|embalaje|tarimas?|cajas)\b|包装|纸箱|木箱|木架|托盘|裸装",
        "package_count": r"\b(?:cartons?|ctns?|boxes?|pallets?|plts?|crates?|packages?|pkgs?|bultos?)\b|箱|托|件",
        "ctns": r"\b(?:cartons?|ctns?|boxes?)\b|箱",
        "line_volume": r"\b(?:cbm|m3|volume)\b|m³|体积|方",
        "shipment_gross_weight": r"\b(?:g\.?\s*w\.?|gross|kg|kgs|weight)\b|毛重|重量|公斤",
    }.items()
}


_COMPACT_TYPED_PACKAGING = re.compile(r"(?<![\w.])[1-9][0-9]*(?:ctns?|plts?)(?![a-z0-9])", re.I)


def _narrow(packet, requirements):
    wanted = set(requirements)
    fields = [r for r in packet["requirements"] if r["requirement_id"] in wanted]
    if not fields:
        return None
    keep = {eid for r in fields for eid in r["evidence_ids"]}
    return {
        **packet,
        "requirements": fields,
        "evidence": [e for e in packet["evidence"] if e["evidence_id"] in keep],
    }


def _decisions(materials, inventory, packets, first_inquiry, control_tags):
    """Return requirement decisions. Inputs contain no label values or verdicts."""
    known = {}
    roles = {}
    totals = Counter()
    for item in inventory:
        row = materials[item["material_tag"]]
        group = item["group"]
        role = (
            "primary"
            if item["kind"] == "body" and first_inquiry[group] == item["seq"]
            else "no_primary_supplement"
            if first_inquiry[group] is None
            else "supplement"
        )
        roles[item["tag"]] = role
        if item["scope"] == "document":
            names = {
                f for f, facts in row["rules"].items() if any(x["status"] == "known" for x in facts)
            }
            known[item["tag"]] = names
            totals.update((group, role, field) for field in names)

    decisions, controls = {}, set()

    def control(slot, tag):
        if control_tags is not None:
            return control_tags.get(slot) == tag
        return slot not in controls

    for item in sorted(inventory, key=lambda r: r["tag"]):
        tag, gid = item["tag"], item["group"]
        if tag not in packets:
            continue
        role = roles[tag]
        packet = packets[tag]
        snippets = {e["evidence_id"]: e["text"] for e in packet["evidence"]}
        per_req = {}
        for req in packet["requirements"]:
            field, rid = req["field_name"], req["requirement_id"]
            if item["scope"] == "cargo_rows":
                # The P0 row adapter only emits requirements for an identified,
                # rule-proven cell and its headers. One job per format is a control.
                slot = ("cargo_rows", item["format"])
                if control(slot, tag) or any(x == "row_control" for x in per_req.values()):
                    controls.add(slot)
                    per_req[rid] = "row_control"
                else:
                    per_req[rid] = "known_row"
            elif field in known[tag]:
                slot = ("document", field)
                if control(slot, tag):
                    controls.add(slot)
                    per_req[rid] = "same_value_control"
                else:
                    per_req[rid] = "known_in_material"
            elif totals[gid, role, field] or (
                role == "supplement" and totals[gid, "primary", field]
            ):
                per_req[rid] = "covered_in_group_role"
            elif (lead := _FREIGHT_LEADS.get(field)) and not any(
                lead.search(snippets[eid])
                or (field == "package_type" and _COMPACT_TYPED_PACKAGING.search(snippets[eid]))
                for eid in req["evidence_ids"]
            ):
                per_req[rid] = "no_field_specific_lead"
            else:
                per_req[rid] = "gap"
        decisions[tag] = {"role": role, "requirements": per_req}
    return decisions


SEND = frozenset({"gap", "same_value_control", "row_control"})


def plan_group_agent_requests(materials, inventory, packets, first_inquiry, *, control_tags=None):
    """Return scoped packets and an audit, using no gold values or model output.

    The caller supplies all rule results for the group (including materials with
    no packet), an explicitly known first inquiry or None, and the packets from
    the main channel's verified evidence projection. This planner only removes
    fields/evidence: the main channel still validates every returned candidate.
    """
    decisions = _decisions(materials, inventory, packets, first_inquiry, control_tags)
    chosen = {
        tag: _narrow(
            packets[tag], [rid for rid, kind in case["requirements"].items() if kind in SEND]
        )
        for tag, case in decisions.items()
    }

    return {tag: packet for tag, packet in chosen.items() if packet is not None}, decisions


class GroupAgentDispatch:
    """Rebind the planned scope to the exact evidence presented by the main channel."""

    def __init__(self, inventory, verified, selected):
        self._verified = deepcopy(verified)
        self._selected = deepcopy(selected)
        self._tags = {}
        for item in inventory:
            if item["tag"] in verified:
                self._tags.setdefault(item["material_tag"], []).append(item["tag"])
        if set(selected) - set(verified):
            raise ValueError("Group Agent selected an unverified request")
        for tag, packet in selected.items():
            narrowed = _narrow(verified[tag], [r["requirement_id"] for r in packet["requirements"]])
            if narrowed != packet:
                raise ValueError("Group Agent selection changed bound evidence")

    def packet_for(self, material_tag, verified_packet):
        if verified_packet is None:
            return None
        matches = [
            tag
            for tag in self._tags.get(material_tag, ())
            if self._verified[tag] == verified_packet
        ]
        if len(matches) != 1:
            raise ValueError("Group Agent request does not match one bound evidence packet")
        return deepcopy(self._selected.get(matches[0]))
