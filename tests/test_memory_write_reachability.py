from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend" / "atlas"
ALLOWED_CREATE_ACTIVE = {
    BACKEND / "memory" / "reconciliation.py",
}


def _create_active_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_active"
    ]


def test_active_durable_memory_creation_is_publisher_only() -> None:
    violations: list[str] = []
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts or path in ALLOWED_CREATE_ACTIVE:
            continue
        for line in _create_active_calls(path):
            violations.append(f"{path.relative_to(BACKEND.parent.parent)}:{line}")
    assert violations == [], (
        "DurableMemoryRepository.create_active is a publication primitive; "
        "production callers outside DerivedMemoryPublisher are forbidden: "
        + ", ".join(violations)
    )
