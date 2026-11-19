import pytest

from doccase_preprocessing.agent_selection import GroupAgentDispatch, plan_group_agent_requests


def test_group_gap_keeps_compact_typed_packaging_and_preserves_source_role():
    inventory = [
        {
            "tag": "primary",
            "material_tag": "primary",
            "group": "g",
            "seq": 1,
            "kind": "body",
            "scope": "document",
        },
        {
            "tag": "reply",
            "material_tag": "reply",
            "group": "g",
            "seq": 2,
            "kind": "body",
            "scope": "document",
        },
        {
            "tag": "attachment",
            "material_tag": "attachment",
            "group": "g",
            "seq": 1,
            "kind": "attachment",
            "scope": "document",
        },
    ]
    materials = {
        "primary": {"rules": {"destination": [{"status": "known"}]}},
        "reply": {"rules": {}},
        "attachment": {"rules": {}},
    }
    fields = [
        {"requirement_id": "TYPE", "field_name": "package_type", "evidence_ids": ["E1"]},
        {"requirement_id": "ROUTE", "field_name": "destination", "evidence_ids": ["E2"]},
    ]
    packets = {
        "reply": {
            "requirements": fields,
            "evidence": [
                {"evidence_id": "E1", "text": "24ctns"},
                {"evidence_id": "E2", "text": "To Barcelona"},
            ],
        },
        "attachment": {
            "requirements": [fields[0]],
            "evidence": [{"evidence_id": "E1", "text": "ref-X24ctnsZ"}],
        },
    }
    selected, audit = plan_group_agent_requests(materials, inventory, packets, {"g": 1})
    assert set(selected) == {"reply"}
    assert selected["reply"]["requirements"] == [fields[0]]
    assert selected["reply"]["evidence"] == [{"evidence_id": "E1", "text": "24ctns"}]
    assert audit["reply"]["role"] == audit["attachment"]["role"] == "supplement"
    assert audit["reply"]["requirements"]["ROUTE"] == "covered_in_group_role"
    dispatch = GroupAgentDispatch(inventory, packets, selected)
    assert dispatch.packet_for("reply", packets["reply"]) == selected["reply"]
    assert dispatch.packet_for("attachment", packets["attachment"]) is None
    with pytest.raises(ValueError, match="does not match"):
        dispatch.packet_for("attachment", packets["reply"])


def test_group_gap_can_request_supplement_when_first_inquiry_missing():
    item = {
        "tag": "reply",
        "material_tag": "reply",
        "group": "g",
        "seq": 2,
        "kind": "body",
        "scope": "document",
    }
    req = {"requirement_id": "TYPE", "field_name": "package_type", "evidence_ids": ["E1"]}
    packet = {"requirements": [req], "evidence": [{"evidence_id": "E1", "text": "2PLT"}]}
    selected, audit = plan_group_agent_requests(
        {"reply": {"rules": {}}}, [item], {"reply": packet}, {"g": None}
    )
    assert selected["reply"] == packet
    assert audit["reply"] == {"role": "no_primary_supplement", "requirements": {"TYPE": "gap"}}
