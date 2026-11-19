"""Runtime inquiry output, independent of annotation and model calls."""

from copy import deepcopy

import pytest

from doccase_preprocessing.derived_fields import verified_density, verified_large_cargo
from doccase_preprocessing.inquiry_runtime import build_inquiry_result


def fact(value, unit, scope="shipment-1", kind="shipment", state="known"):
    return {
        "value": value,
        "unit": unit,
        "status": state,
        "scope_id": scope,
        "object_kind": kind,
        "sources": [{"pointer": "/test/value"}],
    }


def field(name, value, unit, material="body-1"):
    return {
        "name": name,
        "status": "known",
        "value": value,
        "unit": unit,
        "sources": [{"pointer": "/texts/1/text", "material_id": material}],
    }


def job(email_id="m1"):
    return {
        "id": email_id,
        "filename": "mail.eml",
        "state": "complete",
        "parse_scope": "all",
        "route": "docling",
        "email_sha256": email_id,
        "metadata": [
            {
                "email_ref_id": email_id,
                "subject": "Inquiry",
                "from_addr": "a@example.invalid",
                "to_addrs": ["b@example.invalid", "c@example.invalid"],
                "cc_addrs": [],
                "source": {"artifact_kind": "original_eml"},
            }
        ],
        "materials": [
            {
                "id": "body-1",
                "kind": "body",
                "read_bound": True,
                "fields": [
                    field("shipment_gross_weight", 420, "kg"),
                    field("line_volume", 4.581, "CBM"),
                ],
            }
        ],
    }


def group_result(jobs, first=None, system=None):
    from local_web_group import build_group_review

    review = build_group_review(jobs, ("shipment_gross_weight", "line_volume"))
    return build_inquiry_result(review, jobs, confirmed_first_email_id=first, system_result=system)


def test_runtime_headers_require_explicit_first_and_preserve_empty_cc():
    a = job()
    b = job("m2")
    b["metadata"][0]["subject"] = "Reply"
    without = group_result([a, b])
    assert (
        without["first_inquiry"]["email_headers"]["subject"]["status"]
        == "unconfirmed_first_inquiry"
    )
    assert without["emails"][0]["fields"]["cc_addrs"]["status"] == "absent"
    assert without["emails"][0]["fields"]["to_addrs"]["value"] == [
        "b@example.invalid",
        "c@example.invalid",
    ]
    with_first = group_result([a, b], first="m1")
    assert with_first["first_inquiry"]["email_headers"]["subject"]["value"] == "Inquiry"
    assert with_first["first_inquiry"]["email_headers"]["subject"]["object_id"] == "m1"
    assert with_first["shipment"]["fields"]["shipment_density"]["value"] == pytest.approx(
        420 / 4.581
    )
    assert with_first["supplements"][0]["fields"]["shipment_density"]["value"] == pytest.approx(
        420 / 4.581
    )
    unread = deepcopy(a)
    unread["metadata"] = []
    assert group_result([unread])["emails"][0]["fields"]["cc_addrs"]["status"] == "header_not_read"


def test_decoded_eml_preserves_all_to_headers_and_no_cc():
    from doccase_preprocessing.inputs import adapt_eml

    raw = (
        b"Subject: test\r\nFrom: sender@example.invalid\r\n"
        b"To: one@example.invalid, two@example.invalid\r\nTo: three@example.invalid\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nbody"
    )
    bundle = adapt_eml(raw, task_id="test-task", email_ref_id="email-1")
    assert bundle.emails[0]["to_addrs"] == [
        "one@example.invalid",
        "two@example.invalid",
        "three@example.invalid",
    ]
    assert bundle.emails[0]["cc_addrs"] == []


def test_verified_density_requires_same_scope_and_positive_volume():
    assert verified_density(fact(100, "kg"), fact(2, "CBM"))["value"] == 50
    assert (
        verified_density(
            fact(100, "kg", "line-1", "cargo_line"), fact(2, "CBM", "line-2", "cargo_line")
        )["reason"]
        == "scope_mismatch"
    )
    assert verified_density(fact(100, "kg"), None)["reason"] == "volume_missing"
    assert verified_density(fact(100, "kg"), fact(0, "CBM"))["reason"] == "volume_not_positive"
    assert (
        verified_density(fact(100, "kg", state="conflict"), fact(2, "CBM"))["status"] == "conflict"
    )


@pytest.mark.parametrize(
    ("amount", "status", "value"),
    [
        (999, "known", False),
        (1000, "boundary_pending", None),
        (1001, "known", True),
        (None, "uncertain", None),
    ],
)
def test_large_cargo_is_based_only_on_verified_chargeable_weight(amount, status, value):
    result = verified_large_cargo(fact(amount, "kg") if amount is not None else None)
    assert (result["status"], result["value"]) == (status, value)
    first = job()
    if amount is not None:
        first["materials"][0]["fields"].append(field("shipment_chargeable_weight", amount, "kg"))
    result = group_result([first], first="m1")["shipment"]["fields"]["is_large_cargo"]
    assert (result["status"], result["value"]) == (status, value)


def test_system_record_unique_and_group_bound():
    a = job()
    base = group_result([a])
    group_id = base["inquiry_id"]
    assert base["business_system"]["salesperson"]["status"] == "not_connected"
    record = {
        "verified": True,
        "group_id": group_id,
        "inquiry_id": "inq-1",
        "source": {"kind": "business_system", "ref": "system:123"},
        "fields": {
            "salesperson": "Alice",
            "inquiry_created_at": "2099-01-01",
            "internal_reference": "R1",
        },
    }
    result = group_result([a], system={"state": "ok", "matches": [record]})
    assert result["business_system"]["salesperson"]["value"] == "Alice"
    assert result["business_system"]["salesperson"]["object_id"] == "inq-1"
    result = group_result([a], system={"state": "ok", "matches": [record, record]})
    assert result["business_system"]["salesperson"]["status"] == "multiple_matches"
    assert (
        group_result([a], system={"state": "ok", "matches": []})["business_system"]["salesperson"][
            "status"
        ]
        == "no_match"
    )
    assert (
        group_result([a], system={"state": "query_failed"})["business_system"]["salesperson"][
            "status"
        ]
        == "query_failed"
    )


def test_actual_workbench_group_result_recomputes_after_agent_and_keeps_context_supplement(
    tmp_path,
):
    from local_web import Workbench

    first = job("a" * 32)
    row = {
        "worksheet": "Packing",
        "table": "/tables/0",
        "row": 2,
        "ordinal": 1,
        "fields": {
            "line_gross_weight": {
                "value": 100,
                "unit": "kg",
                "status": "known",
                "sources": [{"sheet": "Packing", "cell": "H2"}],
            },
            "line_volume": {
                "value": 2,
                "unit": "CBM",
                "status": "known",
                "sources": [{"sheet": "Packing", "cell": "I2"}],
            },
        },
    }
    first["materials"].append(
        {
            "id": "sheet-1",
            "kind": "attachment",
            "format": "xlsx",
            "fields": [],
            "packing_rows": [row],
        }
    )
    first.update(
        is_email=True,
        origins={},
        replays={},
        details={
            m["id"]: {
                "fields": m["fields"],
                "packing_rows": m.get("packing_rows", []),
                "chain": {"read": {"state": "success"}},
            }
            for m in first["materials"]
        },
    )
    app = Workbench(tmp_path, tmp_path / "python", tmp_path)
    app.jobs[first["id"]] = first
    try:
        before = app.group_review(first["id"], [first["id"]], confirmed_first_email_id=first["id"])
        original_direct = deepcopy(before["group_fields"])
        assert (
            before["structured_result"]["cargo_lines"][0]["fields"]["cargo_line_density"]["value"]
            == 50
        )
        changed = [field("shipment_gross_weight", 400, "kg"), field("line_volume", 4, "CBM")]
        context = {
            "name": "shipment_chargeable_weight",
            "value": 1001,
            "unit": "kg",
            "role": "supplement",
            "email_id": first["id"],
            "material_id": "body-1",
            "sources": [{"pointer": "/texts/3"}],
        }
        app.group_runs[(first["id"],)] = {
            "state": "complete",
            "fields": {(first["id"], "body-1"): changed},
            "context": {"supplements": [context]},
        }
        after = app.group_review(first["id"], [first["id"]], confirmed_first_email_id=first["id"])
        structured = after["structured_result"]
        assert structured["shipment"]["fields"]["shipment_density"]["value"] == 100
        assert structured["shipment"]["fields"]["is_large_cargo"]["value"] is None
        supplementary = next(s for s in structured["supplements"] if s["material_id"] == "body-1")
        assert supplementary["fields"]["is_large_cargo"]["value"] is True
        assert supplementary["role"] == "supplement"
        conflicting = {**context, "name": "shipment_gross_weight", "value": 500}
        app.group_runs[(first["id"],)]["context"]["supplements"].append(conflicting)
        repeated = app.group_review(
            first["id"], [first["id"]], confirmed_first_email_id=first["id"]
        )
        conflict_scope = next(
            s for s in repeated["structured_result"]["supplements"] if s["material_id"] == "body-1"
        )
        assert conflict_scope["fields"]["shipment_density"]["status"] == "conflict"
        assert (
            repeated["structured_result"]["shipment"]["fields"]["shipment_density"]["value"] == 100
        )
        app.business_record_provider = lambda group_id, _ids: {
            "state": "ok",
            "matches": [
                {
                    "verified": True,
                    "group_id": group_id,
                    "inquiry_id": "system-inquiry",
                    "source": {"kind": "business_system", "ref": "system:1"},
                    "fields": {"salesperson": "Alice"},
                }
            ],
        }
        with_system = app.group_review(first["id"], [first["id"]])
        assert (
            with_system["structured_result"]["business_system"]["salesperson"]["value"] == "Alice"
        )

        def failed_provider(*_args):
            raise RuntimeError("query failed")

        app.business_record_provider = failed_provider
        failed = app.group_review(first["id"], [first["id"]])
        assert (
            failed["structured_result"]["business_system"]["salesperson"]["status"]
            == "query_failed"
        )
        assert failed["group_fields"] == with_system["group_fields"]
        assert before["group_fields"] == original_direct
        assert after["group_fields"] != original_direct
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


def test_group_result_http_requires_a_selected_first_inquiry(tmp_path):
    import json
    import threading
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from local_web import LoopbackServer, Workbench, make_handler

    first = job("a" * 32)
    first.update(
        is_email=True,
        origins={},
        replays={},
        details={
            material["id"]: {
                "fields": material["fields"],
                "chain": {"read": {"state": "success"}},
            }
            for material in first["materials"]
        },
    )
    app = Workbench(tmp_path, tmp_path / "python", tmp_path)
    app.jobs[first["id"]] = first
    with LoopbackServer(("127.0.0.1", 0), make_handler(app)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/api/group-result"
            query = f"?email={first['id']}&current={first['id']}"
            with urlopen(base + query, timeout=5) as response:
                assert json.load(response)["first_inquiry"]["email_ref_id"] is None
            with urlopen(base + query + "&first_inquiry=" + first["id"], timeout=5) as response:
                assert json.load(response)["first_inquiry"]["email_ref_id"] == first["id"]
            with pytest.raises(HTTPError) as error:
                urlopen(base + query + "&first_inquiry=" + "b" * 32, timeout=5)
            assert error.value.code == 400
        finally:
            server.shutdown()
            thread.join()
            app.pool.shutdown()
            app.group_pool.shutdown()


def test_each_cargo_line_keeps_its_own_sources_and_missing_volume():
    first = job()
    first["materials"].append(
        {
            "id": "pdf-1",
            "kind": "attachment",
            "fields": [],
            "packing_rows": [
                {
                    "page": 1,
                    "table": "/tables/0",
                    "row": 2,
                    "fields": {
                        "line_gross_weight": {
                            "value": 100,
                            "unit": "kg",
                            "sources": [{"pointer": "/r2/gw"}],
                        },
                        "line_volume": {
                            "value": 2,
                            "unit": "CBM",
                            "sources": [{"pointer": "/r2/v"}],
                        },
                    },
                },
                {
                    "page": 1,
                    "table": "/tables/0",
                    "row": 3,
                    "fields": {
                        "line_gross_weight": {
                            "value": 200,
                            "unit": "kg",
                            "sources": [{"pointer": "/r3/gw"}],
                        }
                    },
                },
            ],
        }
    )
    first_line, second_line = group_result([first], first="m1")["cargo_lines"]
    first_density = first_line["fields"]["cargo_line_density"]
    second_density = second_line["fields"]["cargo_line_density"]
    assert first_density["value"] == 50 and first_density["object_id"] == first_line["object_id"]
    assert {source["pointer"] for source in first_density["sources"]} == {"/r2/gw", "/r2/v"}
    assert second_density["value"] is None and second_density["reason"] == "volume_missing"


def test_runtime_will_not_combine_different_lines_or_use_net_weight():
    first = job()
    first["materials"].append(
        {
            "id": "sheet-1",
            "kind": "attachment",
            "fields": [],
            "packing_rows": [
                {
                    "worksheet": "Packing",
                    "table": "/tables/0",
                    "row": 2,
                    "fields": {
                        "line_gross_weight": {
                            "value": 100,
                            "unit": "kg",
                            "sources": [{"cell": "H2"}],
                        }
                    },
                },
                {
                    "worksheet": "Packing",
                    "table": "/tables/0",
                    "row": 3,
                    "fields": {
                        "line_net_weight": {"value": 80, "unit": "kg", "sources": [{"cell": "G3"}]},
                        "line_volume": {"value": 2, "unit": "CBM", "sources": [{"cell": "I3"}]},
                    },
                },
            ],
        }
    )
    rows = group_result([first], first="m1")["cargo_lines"]
    assert [row["fields"]["cargo_line_density"]["value"] for row in rows] == [None, None]
    assert [row["fields"]["cargo_line_density"]["reason"] for row in rows] == [
        "volume_missing",
        "gross_weight_missing",
    ]


def test_preview_fields_do_not_become_verified_measurements():
    first = job()
    first["materials"][0]["read_bound"] = False
    result = group_result([first], first="m1")["shipment"]["fields"]["shipment_density"]
    assert result["value"] is None and result["reason"] == "read_not_verified"


def test_excel_display_precision_does_not_reduce_density_input_precision():
    first = job()
    first["materials"].append(
        {
            "id": "sheet-1",
            "kind": "attachment",
            "fields": [],
            "packing_rows": [
                {
                    "worksheet": "Packing",
                    "table": "/tables/0",
                    "row": 12,
                    "fields": {
                        "line_gross_weight": {
                            "value": 224,
                            "unit": "kg",
                            "status": "known",
                            "sources": [{"sheet": "Packing", "cell": "H12", "excerpt": "224"}],
                        },
                        "line_volume": {
                            "value": 1.235,
                            "unit": "CBM",
                            "status": "known",
                            "sources": [{"sheet": "Packing", "cell": "I12", "excerpt": "1.23456"}],
                        },
                    },
                },
            ],
        }
    )
    row = group_result([first], first="m1")["cargo_lines"][0]
    assert row["direct_fields"]["line_volume"]["value"] == 1.235
    density = row["fields"]["cargo_line_density"]
    assert density["value"] == pytest.approx(224 / 1.23456)
    assert density["inputs"][1]["value"] == 1.23456


def test_body_shipment_and_line_are_delivered_with_source_role_and_stable_identity():
    first = job()
    first["materials"][0]["body_shipments"] = [
        {
            "object_id": "shipment:body-hash:segment-1",
            "source_role": "current_body",
            "location": {"pointer": "/texts/1/text"},
            "direct_fields": {
                "ctns": {
                    "value": 20,
                    "unit": "carton",
                    "status": "known",
                    "sources": [{"pointer": "/texts/1/text", "excerpt": "20 CTNS"}],
                }
            },
        }
    ]
    first["materials"][0]["packing_rows"] = [
        {
            "table": "/tables/1",
            "row": 3,
            "ordinal": 1,
            "source_role": "current_body",
            "shipment_id": "shipment:body-hash:segment-1",
            "fields": {
                "line_net_weight": {
                    "value": 10,
                    "unit": "kg",
                    "status": "known",
                    "sources": [{"pointer": "/tables/1/data/table_cells/8/text"}],
                }
            },
        }
    ]
    result = group_result([first], first="m1")
    assert result["shipments"][0]["object_id"] == "shipment:body-hash:segment-1"
    assert result["shipments"][0]["direct_fields"]["ctns"]["value"] == 20
    assert result["shipments"][0]["role"] == "first_inquiry"
    line = result["cargo_lines"][0]
    assert line["shipment_id"] == result["shipments"][0]["object_id"]
    assert line["source_role"] == "current_body"
    assert line["direct_fields"]["line_net_weight"]["value"] == 10


def test_first_inquiry_availability_is_independent_of_identity():
    from doccase_preprocessing.inquiry_runtime import first_inquiry_status

    missing = first_inquiry_status(None, {"original_first_inquiry_available": False})
    assert missing["original_availability"] == "not_available_in_archive"
    assert missing["confirmation"] == "unconfirmed"
    assert missing["reason"] == "original_not_in_archive"
    available = first_inquiry_status(None, {"original_first_inquiry_available": True})
    assert available["original_availability"] == "available_in_archive"
    assert available["confirmation"] == "unconfirmed"
    assert first_inquiry_status("mail1")["confirmation_basis"] == "manual_selection"
    assert first_inquiry_status(None)["original_availability"] == "unknown"


def test_quoted_tables_merge_only_with_same_reference_and_complete_row_identity():
    from doccase_preprocessing.inquiry_runtime import _deduplicate_quoted_tables

    def shipment(identifier, status, refs):
        return {
            "object_id": identifier,
            "email_ref_id": identifier,
            "material_id": "0",
            "source_role": "historical_quote",
            "location": {"pointer": "/tables/0"},
            "association": {"status": status, "reference": "AB123"},
            "subject_references": refs,
        }

    def line(identifier, row_hash):
        return {
            "object_id": identifier + "-row",
            "shipment_id": identifier,
            "row_evidence_hash": row_hash,
            "direct_fields": {
                name: {"value": n, "unit": unit, "sources": [{"email_ref_id": identifier}]}
                for name, n, unit in (
                    ("line_net_weight", 10, "kg"),
                    ("line_gross_weight", 12, "kg"),
                    ("line_volume", 1, "CBM"),
                )
            },
        }

    shipments = [
        shipment("first", "verified", ["AB123"]),
        shipment("copy", "unverified", ["AB123"]),
    ]
    merged, lines = _deduplicate_quoted_tables(
        deepcopy(shipments), [line("first", "same"), line("copy", "same")]
    )
    assert len(merged) == len(lines) == 1
    assert len(lines[0]["direct_fields"]["line_net_weight"]["sources"]) == 2
    assert merged[0]["duplicate_evidence"][0]["email_ref_id"] == "copy"
    assert (
        len(
            _deduplicate_quoted_tables(
                deepcopy(shipments), [line("first", "one"), line("copy", "two")]
            )[1]
        )
        == 2
    )
    shipments[1]["subject_references"] = ["AB456"]
    assert (
        len(_deduplicate_quoted_tables(shipments, [line("first", "same"), line("copy", "same")])[1])
        == 2
    )


def test_reply_is_supplement_only_when_reference_matches_confirmed_first_inquiry():
    first, reply = job("first"), job("reply")
    first["metadata"][0]["subject"] = "Inquiry AB-123"
    reply["materials"][0]["body_shipments"] = [
        {
            "object_id": "same",
            "source_role": "current_body",
            "direct_fields": {},
            "association": {"status": "verified", "reference": "AB123"},
        },
        {
            "object_id": "other",
            "source_role": "current_body",
            "direct_fields": {},
            "association": {"status": "verified", "reference": "AB456"},
        },
    ]
    result = group_result([first, reply], first="first")
    assert {item["object_id"]: item["role"] for item in result["shipments"]} == {
        "same": "supplement",
        "other": "reference",
    }
    assert result["shipments"][0]["association"]["group_binding"]["first_email_ref_id"] == "first"
    result = group_result([first, reply])
    assert all(item["role"] == "reference" for item in result["shipments"])
