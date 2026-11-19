import json
from dataclasses import replace

import pytest

from doccase_preprocessing.inputs import adapt_request
from doccase_preprocessing.models import Bundle, Limits, Material, MaterialError, ParserConfig
from doccase_preprocessing.pipeline import prepare


def attachment(mid="one", raw=b"105 kg each", filename="packing.txt", role="current"):
    return Material(mid, f"email-{mid}", role, "attachment", filename, "text/plain", raw)


def test_shared_content_preserves_occurrences_and_separates_same_name_changes(tmp_path):
    first = attachment()
    second = attachment("two", role="ancestor")
    changed = attachment("three", b"110 kg each")
    result = prepare(Bundle("test", [], [first, second, changed]), tmp_path)
    assert len(result["materials"]) == 3
    assert len(result["parse_tasks"]) == 2
    assert result["parse_tasks"][0]["material_ids"] == ["one", "two"]
    assert [r["role"] for r in result["materials"]] == ["current", "ancestor", "current"]
    assert result["materials"][0]["original"] == result["materials"][1]["original"]
    assert not result["parsing_completed"]
    assert not result["business_fields_verified"]
    saved = json.loads((tmp_path / result["manifest_path"]).read_text(encoding="utf-8"))
    assert saved == result
    assert (tmp_path / result["materials"][0]["original"]["path"]).read_bytes() == first.content


def test_missing_and_corrupt_materials_are_not_empty_success(tmp_path):
    missing = replace(attachment("missing"), content=None)
    corrupt = replace(attachment("broken", b"not a pdf", "broken.pdf"), mime="application/pdf")
    result = prepare(Bundle("test", [], [missing, corrupt, attachment()]), tmp_path)
    assert result["state"] == "partial"
    assert result["summary"]["blocked"] == 2
    assert result["summary"]["ready"] == 1
    assert all(r["issues"] for r in result["materials"][:2])
    assert result["materials"][1]["original"] is not None
    assert result["materials"][1]["scope"] is None


def test_declared_size_mismatch_preserves_evidence_without_dispatch(tmp_path):
    result = prepare(Bundle("test", [], [replace(attachment(), declared_size=1)]), tmp_path)
    assert result["state"] == "blocked"
    assert result["materials"][0]["issues"][0]["code"] == "size_mismatch"
    assert result["materials"][0]["original"]["size_bytes"] == len(b"105 kg each")
    assert not result["parse_tasks"]


def test_draft_attachment_checksum_is_verified_when_original_bytes_exist(tmp_path):
    import hashlib

    first = attachment()
    matched = replace(first, metadata={"sha256": hashlib.sha256(first.content).hexdigest().upper()})
    mismatched = replace(attachment("two"), metadata={"sha256": "0" * 64})
    result = prepare(Bundle("test", [], [matched, mismatched]), tmp_path)
    assert result["state"] == "partial"
    assert result["materials"][0]["state"] == "ready"
    assert result["materials"][1]["issues"][0]["code"] == "hash_mismatch"
    assert result["materials"][1]["original"] is not None


def test_parser_config_changes_task_identity_and_output_keeps_previous_runs(tmp_path):
    bundle = Bundle("test", [], [attachment()])
    first = prepare(bundle, tmp_path, parser_config=ParserConfig(version="v1"))
    second = prepare(bundle, tmp_path, parser_config=ParserConfig(version="v2"))
    assert first["parse_tasks"][0]["parse_task_id"] != second["parse_tasks"][0]["parse_task_id"]
    assert first["manifest_path"] != second["manifest_path"]
    assert len(list((tmp_path / "objects").iterdir())) == 1
    assert len(list((tmp_path / "manifests").iterdir())) == 2


def test_untrusted_labels_never_become_paths(tmp_path):
    result = prepare(
        Bundle("../../escape", [], [attachment("../mid", filename="../../escape.txt")]), tmp_path
    )
    original = result["materials"][0]["original"]
    assert (tmp_path / original["path"]).resolve().is_relative_to(tmp_path.resolve())
    assert result["materials"][0]["filename"] == "../../escape.txt"


def test_resolver_is_explicit_and_expected_access_failures_are_per_material(tmp_path):
    remote = replace(
        attachment("remote"), content=None, locator="https://storage.invalid/private?token=secret"
    )
    bundle = Bundle("test", [], [remote, attachment()])
    no_resolver = prepare(bundle, tmp_path)
    assert no_resolver["materials"][0]["issues"][0]["code"] == "acquisition_not_configured"
    assert "token=secret" not in json.dumps(no_resolver)

    def denied(locator, byte_limit):
        assert locator == remote.locator
        assert byte_limit > 0
        raise MaterialError("permission_denied", "Attachment access denied")

    result = prepare(bundle, tmp_path, fetch_attachment=denied)
    assert result["state"] == "partial"
    assert result["materials"][0]["issues"][0]["code"] == "permission_denied"

    def fetched(locator, byte_limit):
        return b"authorized bytes"

    result = prepare(bundle, tmp_path, fetch_attachment=fetched)
    assert result["state"] == "ready"


def test_unexpected_resolver_error_and_original_corruption_propagate(tmp_path):
    remote = replace(attachment(), content=None, locator="https://storage.invalid/private")

    def buggy_resolver(locator, byte_limit):
        raise RuntimeError("resolver implementation failed")

    with pytest.raises(RuntimeError, match="implementation failed"):
        prepare(Bundle("test", [], [remote]), tmp_path, fetch_attachment=buggy_resolver)
    assert not list((tmp_path / "manifests").iterdir())
    bundle = Bundle("test", [], [attachment()])
    result = prepare(bundle, tmp_path)
    original = tmp_path / result["materials"][0]["original"]["path"]
    original.write_bytes(b"corrupted on disk")
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare(bundle, tmp_path)
    assert len(list((tmp_path / "manifests").iterdir())) == 1


def test_public_request_to_manifest_keeps_original_business_evidence(tmp_path):
    from pathlib import Path

    example = Path(__file__).parents[1] / "examples" / "inline-request.json"
    request = json.loads(example.read_text(encoding="utf-8"))
    result = prepare(adapt_request(request), tmp_path)
    assert result["state"] == "ready"
    assert len(result["parse_tasks"]) == 2
    for record in result["materials"]:
        assert record["state"] == "ready"
        assert (tmp_path / record["content"]["path"]).read_bytes()
    assert "315" not in json.dumps(result)
    assert all(task["execution_state"] == "not_started" for task in result["parse_tasks"])


def test_upstream_extracted_text_is_not_mislabeled_as_original_pdf(tmp_path):
    material = replace(
        attachment(raw=b"105 kg each", filename="packing.pdf"),
        mime="application/pdf",
        representation="provided_text",
        declared_size=4096,
    )
    result = prepare(Bundle("test", [], [material]), tmp_path)
    assert result["state"] == "ready"
    record = result["materials"][0]
    assert record["original"] is None
    assert record["content"]["size_bytes"] == len(b"105 kg each")
    assert record["filename"] == "packing.pdf"
    assert record["format"] == "text"
    assert record["issues"][0]["code"] == "original_not_provided"
    assert result["parse_tasks"][0]["route"] == "text"


def test_resolver_receives_remaining_budget_and_stops_when_exhausted(tmp_path):
    materials = [
        replace(attachment(str(index)), content=None, locator=f"authorized:{index}")
        for index in range(4)
    ]
    calls = []
    payloads = {"authorized:0": b"1234567", "authorized:1": b"abc"}

    def fetch(locator, byte_limit):
        calls.append((locator, byte_limit))
        return payloads.get(locator, b"extra")

    result = prepare(
        Bundle("budget", [], materials),
        tmp_path,
        limits=Limits(max_input_bytes=10, max_material_bytes=7),
        fetch_attachment=fetch,
    )
    assert calls == [("authorized:0", 7), ("authorized:1", 3)]
    assert result["state"] == "partial"
    assert [m["state"] for m in result["materials"]] == ["ready", "ready", "blocked", "blocked"]
    for material in result["materials"][2:]:
        assert material["content"] is None
        issue = material["issues"][0]
        assert issue["code"] == "limit_exceeded"
        assert issue["details"] == {"actual": 10, "limit": 10, "resource": "total_bytes"}


def test_interrupted_content_write_leaves_no_published_partial_object(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    material = attachment()
    bundle = Bundle("interrupted", [], [material])
    target = tmp_path / "objects" / hashlib.sha256(material.content).hexdigest()
    original_open = Path.open

    class InterruptedWriter:
        def __init__(self, path, args, kwargs):
            self.stream = original_open(path, *args, **kwargs)

        def __enter__(self):
            return self

        def write(self, raw):
            self.stream.write(raw[:3])
            self.stream.flush()
            raise OSError("interrupted content write")

        def __exit__(self, *args):
            self.stream.close()

    def open_with_interruption(path, *args, **kwargs):
        if args and args[0] == "xb" and path.parent == tmp_path / "objects":
            return InterruptedWriter(path, args, kwargs)
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_with_interruption)
        with pytest.raises(OSError, match="interrupted content write"):
            prepare(bundle, tmp_path)

    assert not target.exists()
    assert not list((tmp_path / "manifests").iterdir())
    assert not list((tmp_path / "objects").iterdir())
    recovered = prepare(bundle, tmp_path)
    assert recovered["state"] == "ready"
    assert target.read_bytes() == material.content
