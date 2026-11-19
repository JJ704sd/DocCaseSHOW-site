from doccase_preprocessing.group_context_agent import (
    bound_context_candidates,
    context_packet,
    first_inquiry_evidence,
    merge_context_response,
)
from doccase_preprocessing.group_context_proofs import same_read_proof
from doccase_preprocessing.group_pipeline import run_group_pipeline


def test_quote_recovery_never_rewrites_scoring_primary():
    quote = {"message_id": "<quote@example.test>", "quote_sha256": "a" * 64}
    proof = {**quote, "original_sha256": "b" * 64, "stage": "新询价", "thread_verified": True}
    pending = first_inquiry_evidence(None, [quote], [{**proof, "thread_verified": False}])
    assert pending["historical_evidence"][0]["status"] == "historical_candidate"
    assert pending["recovered_primary"] == []
    recovered = first_inquiry_evidence(None, [quote], [proof])
    assert recovered["recovered_primary"][0]["status"] == "recovered_primary"
    assert recovered["scoring_primary"] is None
    assert first_inquiry_evidence(17, [quote], [proof])["scoring_primary"] == 17


def test_gap_only_context_keeps_authoritative_role_and_same_shipment():
    target = {
        "id": "Y",
        "name": "shipment_gross_weight",
        "role": "supplement",
        "object": "shipment",
        "unit": "kg",
        "sources": ["packing"],
    }
    sources = [
        {
            "id": "E1",
            "source": "packing",
            "role": "supplement",
            "scope": "one",
            "text": "Total gross weight: 146 kg",
        },
        {
            "id": "E2",
            "source": "email",
            "role": "primary",
            "scope": "one",
            "text": "Attached packing list",
        },
        {
            "id": "E3",
            "source": "quote",
            "role": "historical",
            "scope": "two",
            "text": "Gross weight: 128 kg",
        },
    ]
    assert context_packet([target], sources, resolved={"Y"}) is None
    assert context_packet([target], sources, enabled_fields={"trade_terms"}) is None
    packet = context_packet([target], sources)
    assert packet["requirements"][0]["allowed_sources"] == ["E1"]

    def proof(req, value, cited):
        assert req["role"] == "supplement" and [e["id"] for e in cited] == ["E1"]
        return {"value": 146, "unit": "kg"} if value == 146 else None

    replies = {
        "candidates": [
            {"requirement_id": "Y", "value": 146, "evidence_ids": ["E1", "E2"]},
            {"requirement_id": "Y", "value": 128, "evidence_ids": ["E3"]},
            {"requirement_id": "Y", "value": 146, "evidence_ids": ["E1", "E3"]},
            {"requirement_id": "Y", "value": 128, "evidence_ids": ["E1"]},
        ]
    }
    accepted, rejected = bound_context_candidates(packet, replies, proof)
    assert [(r["value"], r["role"]) for r in accepted] == [(146, "supplement")]
    assert [r["reason"] for r in rejected] == [
        "role_or_shipment_scope_mismatch",
        "role_or_shipment_scope_mismatch",
        "local_proof_missing",
    ]
    updates, _, conflicts = merge_context_response(
        {"Y": [146]}, packet, {"candidates": replies["candidates"][:1]}, proof
    )
    assert updates == {} and conflicts == [{"id": "Y", "reason": "already_established"}]


def test_optional_group_followup_retains_completed_first_pass_on_timeout():
    def follow(group, base):
        assert base == {"retained": group}
        if group == "slow":
            raise TimeoutError()
        return {**base, "context": "candidate"}

    def no_work(*_):
        raise AssertionError("No reads or first-pass requests were scheduled")

    result = run_group_pipeline(
        ["slow", "ready"],
        intake=lambda *_: None,
        read=no_work,
        plan=lambda group, rows: ({"retained": group}, ()),
        model=no_work,
        finish=lambda group, result, calls: result,
        context_followup=follow,
    )
    assert result == {
        "slow": {"retained": "slow"},
        "ready": {"retained": "ready", "context": "candidate"},
    }


def test_group_package_aliases_still_require_explicit_cited_type():
    typed = "一票货物，数据为：2 wooden cases, 160 kg, 1.5 CBM"
    generic = "一票货物，数据为：1 Package, 40 kg, 1 CBM"
    blocks = [
        {"pointer": f"/texts/{i}/text", "text": line} for i, line in enumerate((typed, generic))
    ]
    sources = [
        {
            "id": f"E{i}",
            "source": "body",
            "scope": "one",
            "role": "supplement",
            "text": b["text"],
            "ref": {"pointer": b["pointer"], "start": 0, "end": len(b["text"])},
        }
        for i, b in enumerate(blocks, 1)
    ]
    req = {
        "id": "T",
        "name": "package_type",
        "role": "supplement",
        "object": "shipment",
        "unit": None,
        "sources": ["body"],
    }
    packet = context_packet([req], sources)
    opened = {
        "body": {"row": {"material_id": "body"}, "blocks": blocks, "raw": typed + "\n" + generic}
    }

    def prove(requirement, value, cited):
        return same_read_proof(requirement, value, cited, opened)

    def candidate(value, evidence):
        return {"requirement_id": "T", "value": value, "evidence_ids": [evidence]}

    aliases = [candidate(value, "E1") for value in ("wooden case", "WOODEN_CASE", "wooden-case")]
    accepted, rejected = bound_context_candidates(packet, {"candidates": aliases}, prove)
    assert not rejected
    assert [(r["value"], r["role"], r["locators"]) for r in accepted] == [
        ("wooden_case", "supplement", [blocks[0]["pointer"]])
    ]
    unsupported = [
        candidate("wooden case", "E2"),
        candidate("Package", "E1"),
        candidate("other", "E2"),
        candidate("pallet", "E1"),
    ]
    accepted, rejected = bound_context_candidates(packet, {"candidates": unsupported}, prove)
    assert not accepted
    assert [r["reason"] for r in rejected] == ["local_proof_missing"] * len(unsupported)
