import hashlib
import json
import sys
from pathlib import Path

import pytest
from local_web_group_context import run_context

from local_web import Workbench


def _fixed_body(root, material_id, paragraphs):
    root.mkdir()

    def write(path, raw):
        path.write_bytes(raw)
        return {"ref": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    def save(name, data):
        return write(root / name, json.dumps(data, ensure_ascii=False).encode())

    read = save("docling.json", {"texts": [{"text": p} for p in paragraphs]})
    read_ref = save(
        "read.json",
        {
            "inventory": [{"material_id": material_id, "execution_id": "read-1"}],
            "executions": [
                {
                    "execution_id": "read-1",
                    "ref": read["ref"],
                    "artifact_hash": read["sha256"],
                    "executed_profile": {"reader": "DoclingHtml"},
                }
            ],
        },
    )
    md = write(root / "content.md", paragraphs[1].encode())
    ix = save(
        "index.json",
        {
            "markdown_sha256": md["sha256"],
            "read_sha256": read["sha256"],
            "material_id": material_id,
            "execution_id": "read-1",
            "fragments": [
                {
                    "source_role": "current",
                    "read_pointer": "/texts/1/text",
                    "read_start": 0,
                    "read_end": len(paragraphs[1]),
                    "markdown_start": 0,
                    "markdown_end": len(paragraphs[1]),
                }
            ],
        },
    )
    md_ref = save(
        "markdown.json",
        {
            "materials": {
                material_id: {
                    "state": "success",
                    "markdown_ref": md["ref"],
                    "markdown_sha256": md["sha256"],
                    "index_ref": ix["ref"],
                    "index_sha256": ix["sha256"],
                }
            }
        },
    )
    result_ref = save("result.json", {})
    save(
        "progress.json",
        {"result_ref": result_ref, "read": {"result_ref": read_ref}, "markdown": md_ref},
    )
    raw = write(root / "mail.html", ("".join(f"<p>{p}</p>" for p in paragraphs)).encode())
    record = {
        "material_id": material_id,
        "format": "html",
        "content": {
            "path": "mail.html",
            "sha256": raw["sha256"],
            "size_bytes": (root / "mail.html").stat().st_size,
        },
    }
    return {}, root, record, root, {"read_ref": read_ref, "result_ref": result_ref}, None


@pytest.mark.parametrize("timeout", [False, True])
def test_group_context_uses_fixed_markdown_and_keeps_normal_result(tmp_path, monkeypatch, timeout):
    note = (
        "Synthetic example: pickup handling charges are paid by the buyer under the agreed terms."
    )
    replay = _fixed_body(
        tmp_path / "fixture",
        "m1",
        (
            "Cargo details:",
            note,
            "GW: 40 kgs",
            "Volume: 9 CBM; shipper office address redacted@example.test",
            "----- Original Message -----",
            "GW: 99 kgs",
        ),
    )
    ordinary = {
        "name": "shipment_gross_weight",
        "value": 40,
        "unit": "kg",
        "status": "known",
        "sources": [{"excerpt": "GW: 40 kgs"}],
    }
    calls = []

    class Adapter:
        def extract_material(self, *_args, **_options):
            raise AssertionError("First-pass behavior supplied by fixture")

        def extract_group_context(self, packet, *, timeout_seconds):
            calls.append(packet)
            assert timeout_seconds == 25
            assert [r["name"] for r in packet["requirements"]] == ["business_remarks"]
            evidence = next(s for s in packet["sources"] if s["text"] == note)
            assert evidence["representation"] == "docling_markdown"
            assert evidence["role"] == "supplement"
            assert not any("99 kgs" in s["text"] or "@" in s["text"] for s in packet["sources"])
            if timeout:
                raise TimeoutError()
            return {
                "candidates": [
                    {
                        "requirement_id": "business_remarks",
                        "value": note,
                        "evidence_ids": [evidence["id"]],
                    }
                ]
            }

    app = Workbench(
        tmp_path, Path("unused"), tmp_path, extraction_agent=Adapter(), group_context_fallback=True
    )
    try:
        app.jobs["e1"] = {
            "id": "e1",
            "filename": "trial.eml",
            "is_email": True,
            "state": "complete",
            "route": "deepseek-group",
            "metadata": [],
            "origins": {},
            "parse_scope": "body",
            "materials": [{"id": 0, "kind": "body", "sha256": "sha"}],
            "details": {0: {"fields": []}},
            "replays": {0: replay},
        }

        def first_pass(*_args, **options):
            assert options["replay"][0] == replay[4]
            return (
                None,
                None,
                {
                    "agent": {"events": [{"outcome": "accepted"}]},
                    "structured": {"fields": [ordinary]},
                },
            )

        monkeypatch.setattr(app, "main_result", first_pass)
        assert app.start_group_agent("e1", ["e1"]) == "running"
        app.group_pool.submit(lambda: None).result(timeout=5)
        result = app.group_review("e1", ["e1"])
        agent = result["agent"]
        context = agent["context"]
        assert agent["accepted"] == 1 and agent["state"] == "complete"
        assert context["requests"] == len(calls) == 1
        assert context["model_seconds"] is not None
        assert len(context["historical_candidates"]) == 1
        fields = {f["name"]: f for f in result["group_fields"]}
        assert fields["shipment_gross_weight"]["value"] == 40
        assert fields["business_remarks"]["value"] is None
        if timeout:
            assert context["state"] == "timeout" and context["supplements"] == []
        else:
            [supplement] = context["supplements"]
            assert context["state"] == "complete" and supplement["value"] == note
            assert supplement["role"] == "supplement"
            assert supplement["sources"][0]["pointer"] == "/texts/1/text"
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


def test_group_context_validates_split_current_cargo_volume(tmp_path):
    replay = _fixed_body(
        tmp_path / "fixture",
        "m1",
        (
            "Cargo details:",
            "12 cartons",
            "140,8 kg",
            "0,380 cbm",
            "----- Original Message -----",
            "0,9 cbm",
        ),
    )
    inputs = [("e1", "m1", "body", replay, ())]

    def adapter(packet, *, timeout_seconds):
        assert [s["text"] for s in packet["sources"]] == [
            "Cargo details:",
            "12 cartons",
            "0,380 cbm",
        ]
        return {
            "candidates": [
                {
                    "requirement_id": "line_volume",
                    "value": 0.38,
                    "evidence_ids": [s["id"] for s in packet["sources"]],
                }
            ]
        }

    result = run_context(inputs, ["line_volume"], adapter)
    assert result["state"] == "complete" and result["rejections"] == []
    [supplement] = result["supplements"]
    assert (supplement["value"], supplement["unit"], supplement["role"]) == (
        0.38,
        "CBM",
        "supplement",
    )
    assert [s["pointer"] for s in supplement["sources"]] == [
        "/texts/0/text",
        "/texts/1/text",
        "/texts/3/text",
    ]


def test_group_material_agent_sends_only_evidenced_natural_gaps(tmp_path, monkeypatch):
    sent = []
    returned = []

    class Adapter:
        def extract_material(self, request, *, timeout_seconds):
            sent.append(request)
            return {
                "candidates": [
                    {"requirement_id": "REQ-LINE_VOLUME", "value": 0.38, "evidence_ids": ["E10"]},
                    {"requirement_id": "REQ-DESTINATION", "value": "XYZ", "evidence_ids": ["E10"]},
                ]
            }

    app = Workbench(tmp_path, Path(sys.executable), tmp_path, extraction_agent=Adapter())
    app.jobs["e1"] = {"details": {"0": {"body_shipments": []}}}
    fields = [
        {"name": "line_volume", "status": "uncertain", "value": None},
        {"name": "package_count", "status": "uncertain", "value": None},
        {"name": "shipment_gross_weight", "status": "uncertain", "value": None},
        {"name": "destination", "status": "known", "value": "MAD"},
        {"name": "trade_terms", "status": "conflict", "value": None},
    ]
    request = {
        "requirements": [
            {"requirement_id": f"REQ-{field['name'].upper()}", "field_name": field["name"]}
            for field in fields
        ],
        "evidence": [
            {"evidence_id": "E7", "text": "Cargo details:", "context_for": {"line_volume": 1}},
            {"evidence_id": "E8", "text": "12 cartons", "context_for": {"line_volume": 2}},
            {"evidence_id": "E9", "text": "140,8 kg", "context_for": {"shipment_gross_weight": 3}},
            {"evidence_id": "E10", "text": "0,380 cbm", "context_for": {"line_volume": 4}},
            {"evidence_id": "E11", "text": "Destination: MAD"},
            {"evidence_id": "E12", "text": "Unrelated marketing text"},
        ],
    }

    def main_result(*args, replay, **kwargs):
        returned.append(replay[1](request, timeout_seconds=1))
        return (
            None,
            None,
            {"agent": {"events": [{"stage": "material_agent", "outcome": "received"}]}},
        )

    monkeypatch.setattr(app, "main_result", main_result)
    try:
        app._run_group_agent(("e1",), [("e1", "0", "body", (None,) * 6, ())], {("e1", "0"): fields})
        [packet] = sent
        assert [r["field_name"] for r in packet["requirements"]] == ["line_volume"]
        assert [e["evidence_id"] for e in packet["evidence"]] == ["E7", "E8", "E10"]
        assert returned == [
            {
                "candidates": [
                    {"requirement_id": "REQ-LINE_VOLUME", "value": 0.38, "evidence_ids": ["E10"]},
                ]
            }
        ]
        events = app.group_runs[("e1",)]["validation"][0]["events"]
        assert any(e.get("reason_detail") == "requirement_not_sent" for e in events)
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


@pytest.mark.parametrize(
    ("volume", "proposed"), [("0,380 cbm", 0.2), ("0,380 cbm per carton", 0.38)]
)
def test_group_context_rejects_wrong_or_per_carton_volume(tmp_path, volume, proposed):
    replay = _fixed_body(
        tmp_path / "fixture", "m1", ("Cargo details:", "12 cartons", "140,8 kg", volume)
    )

    def adapter(packet, *, timeout_seconds):
        ids = [s["id"] for s in packet["sources"] if "cbm" in s["text"]]
        return {
            "candidates": [
                {"requirement_id": "line_volume", "value": proposed, "evidence_ids": ids}
            ]
        }

    result = run_context([("e1", "m1", "body", replay, ())], ["line_volume"], adapter)
    assert result["supplements"] == []
    assert result["rejections"] == [{"name": "line_volume", "reason": "local_proof_missing"}]


def test_group_context_does_not_use_historical_volume_as_current(tmp_path):
    replay = _fixed_body(
        tmp_path / "fixture",
        "m1",
        (
            "Please review this inquiry.",
            "Thank you.",
            "----- Original Message -----",
            "Cargo details:",
            "12 cartons",
            "140,8 kg",
            "0,380 cbm",
        ),
    )

    def adapter(*_args, **_kwargs):
        raise AssertionError("Historical evidence must not trigger a current-volume request")

    result = run_context([("e1", "m1", "body", replay, ())], ["line_volume"], adapter)
    assert result["state"] == "no_evidence" and result["requests"] == 0
