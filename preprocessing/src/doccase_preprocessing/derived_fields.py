"""Confirmed local calculations over same-scope cargo measurements."""

from decimal import Decimal, InvalidOperation


def density_kg_per_m3(gross_weight, volume):
    result = {
        "value": None,
        "unit": "kg/m³",
        "status": "uncertain",
        "origin": "calculated",
        "inputs": ["line_gross_weight", "line_volume"],
    }
    if gross_weight is None or volume is None:
        result["reason"] = "gross_weight_or_volume_unknown"
    elif Decimal(str(volume)) <= 0:
        result["reason"] = "volume_not_positive"
    else:
        result.update(
            value=float(Decimal(str(gross_weight)) / Decimal(str(volume))), status="known"
        )
    return result


def is_large_cargo(chargeable_weight):
    return {
        "value": None
        if chargeable_weight is None
        else Decimal(str(chargeable_weight)) >= Decimal("1000"),
        "status": "uncertain" if chargeable_weight is None else "known",
        "origin": "calculated",
        "inputs": ["chargeable_weight"],
        "threshold_kg": 1000,
        "comparison": "greater_than_or_equal",
    }


def _verified_fact(fact, unit):
    if not isinstance(fact, dict) or fact.get("status") != "known":
        return False
    if (
        fact.get("unit") != unit
        or not fact.get("scope_id")
        or not fact.get("sources")
        or fact.get("object_kind") not in {"shipment", "cargo_line"}
        or isinstance(fact.get("value"), bool)
    ):
        return False
    try:
        number = Decimal(str(fact.get("value")))
        return number.is_finite() and number >= 0
    except (InvalidOperation, ValueError):
        return False


def verified_density(gross, volume):
    """Calculate only from independently sourced values bound to the same object."""
    result = {
        "value": None,
        "unit": "kg/m³",
        "status": "uncertain",
        "origin": "calculated",
        "sources": [],
        "inputs": [],
    }
    if any(isinstance(item, dict) and item.get("status") == "conflict" for item in (gross, volume)):
        return {**result, "status": "conflict", "reason": "input_conflict"}
    gross_missing = gross is None or (
        isinstance(gross, dict) and gross.get("value") is None and not gross.get("sources")
    )
    volume_missing = volume is None or (
        isinstance(volume, dict) and volume.get("value") is None and not volume.get("sources")
    )
    if gross_missing or volume_missing:
        return {**result, "reason": "gross_weight_missing" if gross_missing else "volume_missing"}
    if not _verified_fact(gross, "kg") or not _verified_fact(volume, "CBM"):
        return {**result, "reason": "input_unverified"}
    if gross["scope_id"] != volume["scope_id"] or gross["object_kind"] != volume["object_kind"]:
        return {**result, "reason": "scope_mismatch"}
    inputs = [
        {
            "field": item.get("field_name", name),
            "value": item["value"],
            "unit": item["unit"],
            "sources": item["sources"],
        }
        for name, item in (("gross_weight", gross), ("volume", volume))
    ]
    result.update(
        scope_id=gross["scope_id"],
        object_kind=gross["object_kind"],
        inputs=inputs,
        sources=gross["sources"] + volume["sources"],
    )
    if Decimal(str(volume["value"])) <= 0:
        return {**result, "reason": "volume_not_positive"}
    return {
        **result,
        "value": float(Decimal(str(gross["value"])) / Decimal(str(volume["value"]))),
        "status": "known",
        "reason": None,
    }


def verified_large_cargo(chargeable):
    """Preserve the unconfirmed 1000 kg boundary and never substitute gross weight."""
    result = {
        "value": None,
        "status": "uncertain",
        "origin": "calculated",
        "sources": [],
        "unit": None,
        "threshold_kg": 1000,
    }
    if isinstance(chargeable, dict) and chargeable.get("status") == "conflict":
        return {**result, "status": "conflict", "reason": "chargeable_weight_conflict"}
    if chargeable is None or (
        isinstance(chargeable, dict)
        and chargeable.get("value") is None
        and not chargeable.get("sources")
    ):
        return {**result, "reason": "chargeable_weight_missing"}
    if not _verified_fact(chargeable, "kg") or chargeable["object_kind"] != "shipment":
        return {**result, "reason": "chargeable_weight_unverified"}
    number = Decimal(str(chargeable["value"]))
    result.update(
        scope_id=chargeable["scope_id"],
        object_kind="shipment",
        sources=chargeable["sources"],
        inputs=[
            {
                "field": chargeable.get("field_name", "chargeable_weight"),
                "value": chargeable["value"],
                "unit": "kg",
                "sources": chargeable["sources"],
            }
        ],
    )
    if number == Decimal("1000"):
        return {
            **result,
            "status": "boundary_pending",
            "reason": "threshold_requires_confirmation",
            "implementation_value": True,
        }
    return {**result, "value": number > Decimal("1000"), "status": "known", "reason": None}
