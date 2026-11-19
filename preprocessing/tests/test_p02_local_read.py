"""Focused checks for P01 consumption and offline READ binding."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest
from test_inputs import email, request

from doccase_preprocessing.local_handoff import run_local_handoff

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


app = module("p02_local_read", EXAMPLES / "p02_local_read.py")
worker = module("p02_docling_worker", EXAMPLES / "p02_docling_worker.py")


def prepared(tmp_path):
    payload = request(
        email=email(
            body_text="A correction and explicit negation.",
            attachments=[{"material_id": "missing", "filename": "missing.pdf"}],
        )
    )
    return run_local_handoff(
        json.dumps(payload).encode(),
        tmp_path,
        input_format="json",
        request_reference="intake:test",
    )


def test_missing_config_does_not_start_reader_and_keeps_missing_material(tmp_path, monkeypatch):
    result = prepared(tmp_path / "p01")
    manifest = result["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    monkeypatch.setattr(app, "worker", lambda request: pytest.fail("reader must not start"))
    read = app.parse_prepared(
        result["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={body["material_id"]},
        configurations={},
        versions={},
        output_dir=tmp_path / "read",
    )
    assert read["executions"][0]["reason"] == "execution_configuration_missing"
    assert not read["executions"][0]["reader_started"]
    assert (
        next(item for item in read["inventory"] if item["material_id"] == "missing")["read_state"]
        == "blocked"
    )
    missing = next(item for item in read["inventory"] if item["material_id"] == "missing")
    assert missing["preprocessing_issues"][0]["code"] == "missing_content"
    assert not read["intake_bound"]


def test_text_reader_keeps_representation_offsets_and_original_manifest(tmp_path, monkeypatch):
    result = prepared(tmp_path / "p01")
    manifest = result["received"]["manifest"]
    before = result["manifest_path"].read_bytes()
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    monkeypatch.setattr(app, "worker", lambda request: pytest.fail("Docling must not handle text"))
    read = app.parse_prepared(
        result["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=result["request_fingerprint"],
        intake_context=result["intake_context"],
        selected_material_ids={body["material_id"]},
        configurations=app.configurations(),
        versions={},
        output_dir=tmp_path / "read",
    )
    execution = read["executions"][0]
    artifact = json.loads(Path(execution["result"]["json_path"]).read_text(encoding="utf-8"))
    assert artifact["text"] == "A correction and explicit negation."
    assert artifact["location"]["end"] == len(artifact["text"])
    assert artifact["page_location"] is None
    assert execution["occurrence_bindings"][0]["original_sha256"] is None
    assert execution["configured_profile"]["reader"] == "TextReader"
    assert result["manifest_path"].read_bytes() == before
    assert manifest["parse_tasks"][0]["execution_state"] == "not_started"


def test_version_mismatch_is_not_bound():
    configs = app.configurations()
    config = copy.deepcopy(configs[("text", "text")])
    config["versions"]["text_reader"] = "wrong"
    task = {"route": "text", "format": "text", "parser": {"version": "unbound"}}
    assert app.bind(task, config, {}) == "reader_version_mismatch"
    incomplete = copy.deepcopy(configs[("text", "text")])
    del incomplete["requested_scope"]
    assert app.bind(task, incomplete, {}) == "execution_configuration_unsupported"


def test_version_mismatch_does_not_start_selected_plan(tmp_path, monkeypatch):
    result = prepared(tmp_path / "p01")
    manifest = result["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    configs = app.configurations()
    configs[("text", "text")]["versions"]["text_reader"] = "wrong"
    monkeypatch.setattr(app, "text_read", lambda *args: pytest.fail("reader must not start"))
    read = app.parse_prepared(
        result["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={body["material_id"]},
        configurations=configs,
        versions={},
        output_dir=tmp_path / "read",
    )
    assert read["executions"][0]["reason"] == "reader_version_mismatch"
    assert not read["executions"][0]["reader_started"]


def test_missing_local_cache_blocks_only_the_affected_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MODEL_CACHE", tmp_path / "absent")
    configs = app.configurations()
    task = {"route": "docling", "format": "pdf", "parser": {"version": "unbound"}}
    assert app.bind(task, configs[("docling", "pdf")], configs[("docling", "pdf")]["versions"]) == (
        "local_cache_unavailable"
    )
    text_task = {"route": "text", "format": "text", "parser": {"version": "unbound"}}
    assert app.bind(text_task, configs[("text", "text")], {}) is None


def test_failed_docling_plan_is_distinct_from_successful_text_plan(tmp_path, monkeypatch):
    payload = request(
        email=email(
            body_html="<html><body><p>HTML source</p></body></html>",
            attachments=[
                {
                    "material_id": "text",
                    "filename": "extracted.pdf",
                    "mime": "application/pdf",
                    "content_text": "Available text",
                }
            ],
        )
    )
    prepared_result = run_local_handoff(
        json.dumps(payload).encode(),
        tmp_path / "p01",
        input_format="json",
        request_reference="intake:test",
    )
    manifest = prepared_result["received"]["manifest"]
    html = next(
        item
        for item in manifest["materials"]
        if item["kind"] == "body" and item["format"] == "html"
    )
    configs = app.configurations(cache_path=tmp_path)
    versions = configs[("docling", "html")]["versions"]
    monkeypatch.setattr(
        app,
        "worker",
        lambda request: {
            "conversion_status": "failure",
            "errors": [{"error_message": "known conversion failure"}],
            "versions": versions,
            "json_path": None,
            "json_sha256": None,
        },
    )
    read = app.parse_prepared(
        prepared_result["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=None,
        intake_context=None,
        selected_material_ids={html["material_id"], "text"},
        configurations=configs,
        versions=versions,
        output_dir=tmp_path / "read",
    )
    executions = {entry["configured_profile"]["reader"]: entry for entry in read["executions"]}
    assert executions["DoclingHtml"]["execution_state"] == "failure"
    assert (
        executions["DoclingHtml"]["result"]["errors"][0]["error_message"]
        == "known conversion failure"
    )
    assert executions["TextReader"]["execution_state"] == "success"


def test_worker_rechecks_root_and_content_hash(tmp_path):
    root = tmp_path / "trusted"
    root.mkdir()
    (root / "item").write_bytes(b"original")
    expected = hashlib.sha256(b"original").hexdigest()
    assert worker.read_verified(root, "item", expected) == b"original"
    with pytest.raises(ValueError, match="SHA-256"):
        worker.read_verified(root, "item", "0" * 64)
    (tmp_path / "outside").write_bytes(b"original")
    with pytest.raises(ValueError, match="escapes"):
        worker.read_verified(root, "../outside", expected)


def test_visible_html_uses_its_own_stable_character_projection():
    parser = app.VisibleHtml()
    parser.feed("<p>First line</p>\n<script>not visible</script><p>Second line</p>")
    assert [segment["text"] for segment in parser.segments] == ["First line", "Second line"]
    assert [(segment["start"], segment["end"]) for segment in parser.segments] == [
        (0, 10),
        (11, 22),
    ]
    assert parser.visible_offset == 23


@pytest.mark.skipif(
    not os.environ.get("DOCCASE_DOCLING_PYTHON"),
    reason="set DOCCASE_DOCLING_PYTHON to run Docling integration tests",
)
def test_opt_in_text_body_uses_docling_document_and_markdown(tmp_path):
    result = prepared(tmp_path / "p01")
    manifest = result["received"]["manifest"]
    body = next(item for item in manifest["materials"] if item["kind"] == "body")
    versions = app.worker({"operation": "probe", "hard_timeout_seconds": 30})
    configs = app.configurations(declared_versions=versions, docling_text=True)
    read = app.parse_prepared(
        result["manifest_path"],
        tmp_path / "p01",
        expected_task_id=manifest["task_id"],
        expected_request_fingerprint=result["request_fingerprint"],
        intake_context=result["intake_context"],
        selected_material_ids={body["material_id"]},
        configurations=configs,
        versions=versions,
        output_dir=tmp_path / "read",
    )
    execution = read["executions"][0]
    assert execution["executed_profile"]["reader"] == "DoclingText"
    document = json.loads(Path(execution["ref"]).read_text(encoding="utf-8"))
    assert document["texts"][0]["text"] == "A correction and explicit negation."
    records = {x["material_id"]: x for x in manifest["materials"]}
    sidecar = app.export_read_markdown(
        execution, body, records, tmp_path / "p01", tmp_path / "markdown"
    )
    assert "A correction and explicit negation." in Path(sidecar["markdown_ref"]).read_text(
        encoding="utf-8"
    )
