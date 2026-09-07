from __future__ import annotations

import html
import json
import re
from typing import Any

MODEL_OBSERVATION_CHAR_LIMIT = 12_000


def _clean_string(value: str, limit: int) -> str:
    text = value
    if "<" in text and ">" in text and re.search(r"</?[a-zA-Z][^>]*>", text):
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"… [model projection omitted {omitted} characters]"


def _bound(value: Any, *, string_limit: int, list_limit: int, depth: int = 0) -> Any:
    if depth >= 6:
        return "[nested model evidence omitted]"
    if isinstance(value, str):
        return _clean_string(value, string_limit)
    if isinstance(value, list):
        items = [_bound(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1) for item in value[:list_limit]]
        if len(value) > list_limit:
            items.append({"model_projection_omitted_items": len(value) - list_limit})
        return items
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() == "data_base64":
                continue
            result[str(key)] = _bound(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1)
        return result
    return value


def bound_model_evidence(value: Any, *, char_limit: int = MODEL_OBSERVATION_CHAR_LIMIT) -> tuple[Any, dict[str, int] | None]:
    """Return a bounded model-facing copy plus omission metadata when compaction occurred."""
    original = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    first = _bound(value, string_limit=6_000, list_limit=24)
    encoded = json.dumps(first, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) <= char_limit:
        metadata = None
        if len(encoded) < len(original):
            metadata = {"original_characters": len(original), "projected_characters": len(encoded)}
        return first, metadata

    second = _bound(value, string_limit=1_800, list_limit=10)
    encoded = json.dumps(second, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) <= char_limit:
        return second, {"original_characters": len(original), "projected_characters": len(encoded)}

    if isinstance(second, dict):
        structural = {
            "status": second.get("status"),
            "operation_id": second.get("operation_id"),
            "message": _clean_string(str(second.get("message") or ""), 1200),
            "output_keys": sorted(str(key) for key in second.get("output", {})) if isinstance(second.get("output"), dict) else [],
        }
    else:
        structural = {"kind": type(second).__name__}
    return structural, {"original_characters": len(original), "projected_characters": len(json.dumps(structural, default=str))}


def attach_projection_metadata(value: Any, metadata: dict[str, int] | None) -> Any:
    if metadata is None or not isinstance(value, dict):
        return value
    result = dict(value)
    result["model_projection"] = {
        "compacted": True,
        **metadata,
        "canonical_evidence_retained": True,
    }
    return result
