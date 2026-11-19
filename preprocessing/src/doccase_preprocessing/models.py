"""Internal handoff types; these are not the public AI service schema."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Limits:
    # Local operating defaults, not production SLA or model token limits.
    max_input_bytes: int = 20 * 1024 * 1024
    max_material_bytes: int = 20 * 1024 * 1024
    max_materials: int = 200
    max_archive_entries: int = 2000
    max_unpacked_bytes: int = 64 * 1024 * 1024
    max_pdf_pages: int = 1000
    max_image_pixels: int = 40_000_000

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class InputError(ValueError):
    """The request envelope cannot be accepted as an input bundle."""


class MaterialError(Exception):
    """An expected failure of one material, safe to expose without credentials."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.issue = Issue(code, message, details)


@dataclass
class Material:
    material_id: str
    email_ref_id: str
    role: Literal["current", "ancestor", "supplementary"]
    kind: Literal["body", "attachment", "inline", "original_email"]
    filename: str
    mime: str
    content: bytes | None = None
    locator: str | None = None
    declared_size: int | None = None
    cid: str | None = None
    selected: bool = True
    alternative_group: str | None = None
    parent_material_id: str | None = None
    issues: list[Issue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    representation: Literal["original", "provided_text", "derived_text"] = "original"


@dataclass
class Bundle:
    task_id: str
    emails: list[dict[str, Any]]
    materials: list[Material]
    issues: list[Issue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Inspection:
    format: str
    route: str | None
    scope: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)


@dataclass(frozen=True)
class ParserConfig:
    # Included in task identity. This describes a proposed downstream parser, not an execution.
    name: str = "docling"
    version: str = "unbound"
    options: dict[str, Any] = field(default_factory=dict)
