from __future__ import annotations

import re
from typing import Any

_SUPPRESSED = "[suppressed by owner memory directive]"


def guarded_contents(values) -> list[str]:
    unique = {" ".join(str(value or "").split()) for value in values}
    return sorted((value for value in unique if value), key=len, reverse=True)


def redact_guarded_text(text: str, contents) -> str:
    redacted = text
    for content in guarded_contents(contents):
        redacted = re.sub(re.escape(content), _SUPPRESSED, redacted, flags=re.IGNORECASE)
    return redacted


def redact_guarded_value(value: Any, contents):
    guards = guarded_contents(contents)
    if not guards:
        return value
    if isinstance(value, str):
        return redact_guarded_text(value, guards)
    if isinstance(value, list):
        return [redact_guarded_value(item, guards) for item in value]
    if isinstance(value, dict):
        return {key: redact_guarded_value(item, guards) for key, item in value.items()}
    return value
