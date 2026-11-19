"""Atomic, immutable local material-result JSON snapshots, not a delivery service."""

import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .output_consume import assess_handoff, validation_inputs


class SnapshotError(Exception):
    """A snapshot could not be validated, saved or read."""


class SnapshotValidationError(SnapshotError):
    """Snapshot data or its referenced evidence did not validate."""


class SnapshotStorageError(SnapshotError):
    """The atomic snapshot operation failed."""


class SnapshotConflict(SnapshotError):
    """The requested revision already exists or has an invalid baseline."""


def _encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _path(root, result_id, revision):
    if (
        not isinstance(result_id, str)
        or not result_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise ValueError("A result_id and positive integer revision are required")
    key = hashlib.sha256(result_id.encode("utf-8")).hexdigest()
    return Path(root).resolve() / key / f"{revision}.json"


def commit_result(
    root,
    snapshot,
    *,
    task_scope,
    manifest_ref,
    field_profile,
    read_ref,
    content_root,
    field_contract_ref,
    _expected_base=None,
):
    """Validate before atomically publishing; initial callers omit the internal baseline."""
    context = validation_inputs(
        task_scope=task_scope,
        manifest_ref=manifest_ref,
        field_profile=field_profile,
        read_ref=read_ref,
        content_root=content_root,
        field_contract_ref=field_contract_ref,
    )
    try:
        # Validate and publish the same frozen object even if the caller keeps
        # mutating its original in another thread.
        snapshot = copy.deepcopy(snapshot)
        assessment = assess_handoff(snapshot, **context)
        handoff = snapshot["material_handoff"]
        revision = handoff["result_revision"]
        if handoff["base_revision"] != _expected_base or revision != (
            1 if _expected_base is None else _expected_base + 1
        ):
            raise SnapshotConflict("Snapshot revision does not follow the required base revision")
        target = _path(root, handoff["result_id"], handoff["result_revision"])
        payload = {"snapshot": snapshot, "validation_inputs": context}
        envelope = {
            "format": "material-result-snapshot/1",
            "payload_sha256": hashlib.sha256(_encode(payload)).hexdigest(),
            "payload": payload,
        }
        raw = _encode(envelope) + b"\n"
    except SnapshotError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise SnapshotValidationError(f"Material result validation failed: {error}") from error

    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        revisions = [
            int(path.stem) for path in target.parent.glob("*.json") if path.stem.isdecimal()
        ]
        latest = max(revisions, default=None)
        if latest != _expected_base:
            raise SnapshotConflict("Material result revision already exists or base is stale")
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Linking a complete file publishes it without ever replacing an existing revision.
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise SnapshotConflict("Material result revision already exists") from error
    except OSError as error:
        raise SnapshotStorageError(f"Atomic material result save failed: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()
    return {**assessment, "snapshot_ref": str(target), "payload_sha256": envelope["payload_sha256"]}


def _read_entry(root, result_id, revision):
    try:
        target = _path(root, result_id, revision)
        envelope = json.loads(target.read_bytes())
        payload = envelope["payload"]
        if (
            envelope["format"] != "material-result-snapshot/1"
            or hashlib.sha256(_encode(payload)).hexdigest() != envelope["payload_sha256"]
        ):
            raise ValueError("Snapshot payload SHA-256 mismatch")
        snapshot = payload["snapshot"]
        handoff = snapshot["material_handoff"]
        if handoff["result_id"] != result_id or handoff["result_revision"] != revision:
            raise ValueError("Snapshot identity differs from requested revision")
        assess_handoff(snapshot, **payload["validation_inputs"])
        return snapshot, payload["validation_inputs"]
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise SnapshotValidationError(f"Material result readback failed: {error}") from error


def read_result(root, result_id, revision):
    """Retrieve exactly one revision and revalidate its saved external evidence."""
    return _read_entry(root, result_id, revision)[0]
