"""Read displayable fields from a JSON prefix without completing unfinished objects."""
from __future__ import annotations

import json

from pydantic_core import from_json

from .models import Finding, ReviewItem


def report_json(text: str) -> str:
    text = text.lstrip()
    if text.startswith("```"):
        _, separator, text = text.partition("\n")
        if not separator:
            return ""
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3]
    return text


def validate_references(item, source_ids: set[str], fragment_ids: set[str], path: str = "item"):
    for field, allowed in (("source_ids", source_ids), ("fragment_ids", fragment_ids)):
        if not set(getattr(item, field)) <= allowed:
            raise ValueError(f"Неизвестная ссылка: {path}.{field}.")
    if isinstance(item, ReviewItem):
        if not set(item.dependencies) <= fragment_ids:
            raise ValueError(f"Неизвестная ссылка: {path}.dependencies.")
        if item.rationale_kind == "recorded" and not item.source_ids:
            raise ValueError(f"У записанного решения отсутствует источник: {path}.source_ids.")
        item.reviewed = False


def preview(text: str, source_ids: set[str], fragment_ids: set[str], prefix: str) -> dict:
    """Only summary strings may be partial. raw_decode requires complete card objects.

    Parsing the buffered prefix at the publication cadence also handles fields in any
    order and split escapes, without mistaking braces inside strings for boundaries.
    The entire document is still validated independently before publication.
    """
    result = {"summary": "", "items": [], "findings": []}
    text = report_json(text)
    decoder = json.JSONDecoder()
    pos = 0

    def whitespace():
        nonlocal pos
        while pos < len(text) and text[pos].isspace():
            pos += 1

    try:
        if not text.startswith("{"):
            return result
        pos = 1
        while True:
            whitespace()
            if pos >= len(text) or text[pos] == "}":
                break
            key, pos = decoder.raw_decode(text, pos)
            whitespace()
            if pos >= len(text) or text[pos] != ":":
                break
            pos += 1
            whitespace()
            if key in ("items", "findings") and text[pos:pos + 1] == "[":
                pos += 1
                result[key] = []
                index = 0
                while True:
                    whitespace()
                    if text[pos:pos + 1] == "]":
                        pos += 1
                        break
                    value, pos = decoder.raw_decode(text, pos)
                    try:
                        item = (ReviewItem if key == "items" else Finding).model_validate(value)
                        validate_references(item, source_ids, fragment_ids)
                        item.id = f"{prefix}:{key}:{index}"
                        result[key].append(item.model_dump())
                    except ValueError:
                        pass  # The full validator will request a repair; never show this card.
                    index += 1
                    whitespace()
                    if text[pos:pos + 1] != ",":
                        if text[pos:pos + 1] == "]":
                            pos += 1
                        else:
                            return result
                        break
                    pos += 1
            else:
                try:
                    value, end = decoder.raw_decode(text, pos)
                except ValueError:
                    if key == "summary" and text[pos:pos + 1] == '"':
                        value = from_json(text[pos:], allow_partial="trailing-strings")
                        if isinstance(value, str):
                            result["summary"] = value
                    break
                pos = end
                if key == "summary" and isinstance(value, str):
                    result["summary"] = value
            whitespace()
            if text[pos:pos + 1] != ",":
                break
            pos += 1
    except (ValueError, TypeError):
        pass  # An incomplete or malformed prefix must not stop receipt of the rest.
    return result
