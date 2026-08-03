"""Immutable published template catalog for AI Edit V3."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import canonical_json


_ROOT = Path(__file__).resolve().parent
_CATALOG = _ROOT / "templates-v1.json"
_PREVIEWS = _ROOT / "template-previews"
_RATIOS = frozenset({"16:9", "9:16"})
_DIRECTIONS = frozenset({"commercial_diagnostic", "editorial_explainer"})


@dataclass(frozen=True)
class TemplateContract:
    template_id: str
    version: str
    status: str
    title: str
    category: str
    creative_direction: str
    ratio: str
    supported_ratios: tuple[str, ...]
    allowed_layouts: tuple[str, ...]
    preview_file: str
    preview_path: Path
    preview_sha256: str
    preview_cos_key: str
    capabilities: Mapping[str, Any]
    sha256: str

    def with_changes(self, **changes: Any) -> "TemplateContract":
        values = _source_values(self)
        values.update(changes)
        values.pop("preview_path", None)
        values.pop("sha256", None)
        return _contract(values)


def load_template_catalog() -> tuple[TemplateContract, ...]:
    raw = json.loads(_CATALOG.read_text(encoding="utf-8"))
    if set(raw) != {"version", "templates"} or raw["version"] != "1" or not isinstance(raw["templates"], list):
        raise RuntimeError("template_catalog_invalid")
    contracts = tuple(_contract(item) for item in raw["templates"])
    identities = [(item.template_id, item.version) for item in contracts]
    if len(identities) != len(set(identities)) or identities != sorted(identities):
        raise RuntimeError("template_catalog_identity_invalid")
    return contracts


def _contract(raw: Mapping[str, Any]) -> TemplateContract:
    required = {
        "template_id", "version", "status", "title", "category", "creative_direction", "ratio",
        "supported_ratios", "allowed_layouts", "preview_file", "preview_sha256", "preview_cos_key", "capabilities",
    }
    if set(raw) != required:
        raise RuntimeError("template_contract_fields_invalid")
    template_id = raw["template_id"]
    preview_file = raw["preview_file"]
    if not isinstance(template_id, str) or not template_id or not isinstance(preview_file, str) or Path(preview_file).name != preview_file:
        raise RuntimeError("template_contract_identity_invalid")
    ratio = raw["ratio"]
    supported = tuple(raw["supported_ratios"])
    allowed = tuple(raw["allowed_layouts"])
    if ratio not in _RATIOS or supported != (ratio,) or len(allowed) < 2 or len(allowed) != len(set(allowed)):
        raise RuntimeError("template_contract_capability_invalid")
    if raw["creative_direction"] not in _DIRECTIONS or raw["status"] not in {"draft", "published", "retired"}:
        raise RuntimeError("template_contract_state_invalid")
    preview_path = (_PREVIEWS / preview_file).resolve(strict=True)
    if preview_path.parent != _PREVIEWS.resolve(strict=True):
        raise RuntimeError("template_preview_path_invalid")
    preview_digest = hashlib.sha256(preview_path.read_bytes()).hexdigest()
    if preview_digest != raw["preview_sha256"]:
        raise RuntimeError("template_preview_hash_mismatch")
    capabilities = raw["capabilities"]
    if not isinstance(capabilities, dict) or not capabilities:
        raise RuntimeError("template_capabilities_invalid")
    canonical = {key: raw[key] for key in sorted(required)}
    digest = hashlib.sha256(canonical_json(canonical)).hexdigest()
    return TemplateContract(
        template_id=template_id,
        version=raw["version"],
        status=raw["status"],
        title=raw["title"],
        category=raw["category"],
        creative_direction=raw["creative_direction"],
        ratio=ratio,
        supported_ratios=supported,
        allowed_layouts=allowed,
        preview_file=preview_file,
        preview_path=preview_path,
        preview_sha256=preview_digest,
        preview_cos_key=raw["preview_cos_key"],
        capabilities=MappingProxyType(dict(capabilities)),
        sha256=digest,
    )


def _source_values(contract: TemplateContract) -> dict[str, Any]:
    return {
        "template_id": contract.template_id,
        "version": contract.version,
        "status": contract.status,
        "title": contract.title,
        "category": contract.category,
        "creative_direction": contract.creative_direction,
        "ratio": contract.ratio,
        "supported_ratios": list(contract.supported_ratios),
        "allowed_layouts": list(contract.allowed_layouts),
        "preview_file": contract.preview_file,
        "preview_sha256": contract.preview_sha256,
        "preview_cos_key": contract.preview_cos_key,
        "capabilities": dict(contract.capabilities),
    }
