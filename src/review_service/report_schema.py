"""The model's output contract, constrained to the evidence in one request."""
from __future__ import annotations

from pydantic import ValidationError

from .models import GeneratedReport


def report_schema(source_ids: set[str], fragment_ids: set[str]) -> dict:
    schema = GeneratedReport.model_json_schema()
    for name in ("ReviewItem", "Finding"):
        properties = schema["$defs"][name]["properties"]
        properties.pop("id")
        properties.pop("reviewed", None)
        for field, allowed in (("source_ids", source_ids), ("fragment_ids", fragment_ids),
                               ("dependencies", fragment_ids)):
            if field in properties:
                array = properties[field]
                if allowed:
                    array["items"] = {"type": "string", "enum": sorted(allowed)}
                else:
                    array["maxItems"] = 0
        if not source_ids and name == "ReviewItem":
            properties["rationale_kind"]["enum"] = ["reconstructed", "unknown"]

    def normalize(node):
        node.pop("title", None)
        node.pop("default", None)
        if "properties" in node:
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
            for child in node["properties"].values():
                normalize(child)
        for child in node.get("$defs", {}).values():
            normalize(child)
        if isinstance(node.get("items"), dict):
            normalize(node["items"])

    normalize(schema)
    return schema


def validation_detail(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_url=False)
        if any(error["type"] == "json_invalid" for error in errors):
            return "Некорректный JSON в ответе модели."
        fields = [".".join(map(str, error["loc"])) or "report" for error in errors[:5]]
        return "Нарушена структура отчёта: " + ", ".join(fields) + "."
    return str(exc)
