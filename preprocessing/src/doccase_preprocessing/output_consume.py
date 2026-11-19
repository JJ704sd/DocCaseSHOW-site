"""Validate a P03 material result against inputs supplied independently by its caller."""

import copy
from pathlib import Path

from .main_channel import validate_material_handoff


def assess_handoff(
    snapshot, *, task_scope, manifest_ref, field_profile, read_ref, content_root, field_contract_ref
):
    """Validate against independently supplied output-consumer inputs."""
    if field_profile is None:
        raise ValueError("required_field_profile_missing")
    if task_scope is None:
        raise ValueError("An independent task scope is required")
    return validate_material_handoff(
        snapshot,
        task_scope=task_scope,
        manifest_ref=manifest_ref,
        field_profile=field_profile,
        read_ref=read_ref,
        content_root=content_root,
        field_contract_ref=field_contract_ref,
    )


def validation_inputs(
    *, task_scope, manifest_ref, field_profile, read_ref, content_root, field_contract_ref
):
    """Freeze the independently supplied, non-secret inputs for future readback."""
    return copy.deepcopy(
        {
            "task_scope": task_scope,
            "manifest_ref": manifest_ref,
            "field_profile": field_profile,
            "read_ref": read_ref,
            "content_root": str(Path(content_root).resolve()),
            "field_contract_ref": field_contract_ref,
        }
    )
